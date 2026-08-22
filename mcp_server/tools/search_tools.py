"""search_knowledge — the general-purpose entry point."""
from .. import db
from ..limits import clamp_limit, require_non_empty
from ..logging_setup import tool_boundary
from ..queries import search_queries


@tool_boundary("search_knowledge")
def search_knowledge(query: str, limit: int = None) -> dict:
    """General-purpose natural-language-ish search across the knowledge
    graph: business flows, APIs, services, and classes/components, ranked
    by relevance with business-flow/relationship results preferred over
    isolated nodes. Start here when unsure which specific tool to use."""
    query = require_non_empty(query, "query")
    config = db.get_config()
    limit = clamp_limit(limit, config)
    with db.connect() as conn:
        results = search_queries.search_knowledge(conn, query, limit)
    return {"query": query, "results": results}


def register(server):
    server.tool()(search_knowledge)
