"""Knowledge Graph MCP server — entry point.

    python mcp_server/server.py

Reads KG_DB_PATH (and the other MCP_* settings) from the environment —
see mcp-config.example.env and MCP.md. Serves over stdio by default,
which is what Claude Code/Desktop and most local MCP clients expect;
HTTP/SSE transport is available from the same MCPServer instance
(server.run("streamable-http")) without touching the tool layer at all,
should that ever be needed.
"""
from mcp.server import MCPServer

from .config import load_config
from .logging_setup import configure_logging
from .tools import register_all

INSTRUCTIONS = (
    "Read-only semantic access to a code knowledge graph built from Java/Spring, "
    "Angular, and React source. Start with search_knowledge or get_context_pack "
    "for a topic; use get_service_context / get_api_context / get_business_flow "
    "for a specific target you already know the name of; use find_service, "
    "find_api, find_callers, or find_dependencies to locate a name first if "
    "you're not sure of it. Call get_source_evidence only when you need to point "
    "at or quote the real implementation — it's the most expensive call. Every "
    "tool returns a compact summary, never the full graph or full source files."
)


def create_server():
    config = load_config()
    configure_logging(config.log_level)
    server = MCPServer(
        config.server_name,
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )
    register_all(server)
    return server


def main():
    server = create_server()
    server.run("stdio")


if __name__ == "__main__":
    main()
