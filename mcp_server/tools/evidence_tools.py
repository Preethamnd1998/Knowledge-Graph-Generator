"""get_source_evidence.

Returns a precise pointer into the original source (file + line range +
the short declaration signature the indexer already captured) — not the
file's full contents, and not fetched fresh from disk. The MCP server
only ever talks to the knowledge_graph.db (see MCP.md architecture); it
doesn't read back into the customer source tree, so this is exactly what
the graph itself knows, nothing reconstructed or guessed.
"""
from .. import db
from ..errors import NotFoundError
from ..limits import require_non_empty
from ..logging_setup import tool_boundary
from ..queries.common import find_node_exact, find_nodes_fuzzy, get_parent_class


@tool_boundary("get_source_evidence")
def get_source_evidence(node: str) -> dict:
    """Implementation evidence for ONE node (class/method/component/API
    handler): file path, line range, and its captured signature. Use only
    when you need to point at or quote the real implementation — for
    everything else, get_service_context / get_api_context / get_business_flow
    are far cheaper."""
    node = require_non_empty(node, "node")
    with db.connect() as conn:
        row = find_node_exact(conn, node)
        if not row:
            matches = find_nodes_fuzzy(conn, node, limit=1)
            row = matches[0] if matches else None
        if not row:
            raise NotFoundError(f"'{node}' was not found in the knowledge graph.")

        parent_class = None
        if row["node_type"] == "METHOD":
            parent = get_parent_class(conn, row["id"])
            parent_class = parent["name"] if parent else None

    return {
        "node_type": row["node_type"],
        "name": row["name"],
        "service": row.get("service"),
        "file": row.get("file_path"),
        "class": parent_class,
        "method": row["name"] if row["node_type"] == "METHOD" else None,
        "line_start": row.get("start_line"),
        "line_end": row.get("end_line"),
        "signature": row.get("signature"),
        "summary": f"{row['node_type']} '{row['name']}' in {row.get('service') or 'unknown service'}"
                   + (f" ({row['file_path']}:{row['start_line']})" if row.get("file_path") and row.get("start_line") else ""),
    }


def register(server):
    server.tool()(get_source_evidence)
