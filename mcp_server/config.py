"""Configuration for the Knowledge Graph MCP server — read entirely from
environment variables (the way an MCP client like Claude Code/Desktop
injects config into a spawned server's `env`), with defaults that match
`mcp-config.example.env`. No config file parser here: unlike the indexer's
kg-config.env, there is nothing here a non-technical user needs to
comment/uncomment, so plain env vars are the simpler, more standard fit
for an MCP server's config surface.
"""
import os
from dataclasses import dataclass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_int(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    db_path: str
    server_name: str
    log_level: str
    max_results: int
    max_depth: int
    max_context_tokens: int
    query_timeout_seconds: float


def load_config() -> Config:
    db_path = os.environ.get("KG_DB_PATH", "./output/knowledge_graph.db")
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(os.path.join(_REPO_ROOT, db_path))

    return Config(
        db_path=db_path,
        server_name=os.environ.get("MCP_SERVER_NAME", "knowledge-graph"),
        log_level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
        max_results=_env_int("MCP_MAX_RESULTS", 10),
        max_depth=_env_int("MCP_MAX_DEPTH", 3),
        max_context_tokens=_env_int("MCP_MAX_CONTEXT_TOKENS", 3000),
        query_timeout_seconds=float(os.environ.get("MCP_QUERY_TIMEOUT_SECONDS", "5")),
    )
