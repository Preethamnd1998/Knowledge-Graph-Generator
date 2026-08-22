"""Read-only access to a knowledge_graph.db produced by ../indexer.py.

Opened strictly read-only at the SQLite level (``mode=ro`` in the
connection URI) so a bug in a query module can never write to, let alone
corrupt, the graph — this is enforced by SQLite itself, not just by
"we didn't write a write tool". A per-connection progress handler also
aborts any single query that runs unreasonably long (``MCP_QUERY_TIMEOUT_SECONDS``).
"""
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import quote

from .config import load_config
from .errors import DatabaseNotFoundError, DatabaseUnavailableError, QueryTimeoutError

logger = logging.getLogger("kg_mcp.db")


def get_config():
    """Reloaded from the environment on every call (cheap — a handful of
    os.environ reads) rather than cached at import time, specifically so
    tests can monkeypatch KG_DB_PATH/MCP_* per-test without import-order
    games. A long-running server pays this cost once per tool call, which
    is negligible next to the SQLite query itself."""
    return load_config()


@contextmanager
def connect():
    """Yield a read-only sqlite3 connection with row access by column name.

    Raises DatabaseNotFoundError if the configured KG_DB_PATH doesn't
    exist, DatabaseUnavailableError for any other open failure (e.g. the
    file exists but isn't a valid SQLite database).
    """
    config = get_config()
    path = config.db_path
    if not os.path.isfile(path):
        raise DatabaseNotFoundError(
            f"Knowledge graph database not found at '{path}'. Run "
            f"'python indexer.py' first, or set KG_DB_PATH to an existing .db file."
        )

    uri = f"file:{quote(path)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError(f"Could not open knowledge graph database: {exc}") from exc

    conn.row_factory = sqlite3.Row
    start = time.monotonic()
    timeout = config.query_timeout_seconds

    def _progress_handler():
        return (time.monotonic() - start) > timeout

    conn.set_progress_handler(_progress_handler, 1000)

    try:
        yield conn
    finally:
        conn.close()


def fetch_all(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise QueryTimeoutError("Query exceeded the configured time limit.") from exc
        logger.exception("Query failed: %s", sql)
        raise DatabaseUnavailableError("A database query failed.") from exc


def fetch_one(conn, sql, params=()):
    rows = fetch_all(conn, sql, params)
    return rows[0] if rows else None


def row_to_dict(row):
    return dict(row) if row is not None else None
