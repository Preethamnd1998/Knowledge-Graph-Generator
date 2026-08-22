"""find_service, get_service_context, get_service_dependencies."""
from .. import db
from ..errors import NotFoundError
from ..limits import clamp_depth, clamp_limit, require_non_empty, validate_direction
from ..logging_setup import tool_boundary
from ..queries import service_queries


@tool_boundary("find_service")
def find_service(query: str, limit: int = None) -> dict:
    """Find services (Java microservices or Angular/React sub-projects)
    matching a name or keyword. Returns compact summaries only — use
    get_service_context for details on a specific match."""
    query = require_non_empty(query, "query")
    config = db.get_config()
    limit = clamp_limit(limit, config)
    with db.connect() as conn:
        results = service_queries.search_services(conn, query, limit)
    return {"results": results}


@tool_boundary("get_service_context")
def get_service_context(service: str) -> dict:
    """Compact overview of one service: purpose, technology, API/entity/
    table counts, cross-service dependencies, and naming-convention-
    detected domain events. Does not return source code or a full API list —
    use find_api / get_api_context for that."""
    service = require_non_empty(service, "service")
    with db.connect() as conn:
        context = service_queries.get_context(conn, service)
    if context is None:
        raise NotFoundError(f"No service named '{service}' found in the knowledge graph.")
    return context


@tool_boundary("get_service_dependencies")
def get_service_dependencies(service: str, direction: str = "outgoing", depth: int = 1) -> dict:
    """Compact service-level dependency tree — which other services this
    one depends on (direction='outgoing'), depend on it ('incoming'), or
    both. Depth is capped server-side; circular relationships can't cause
    infinite traversal (each service is visited at most once)."""
    service = require_non_empty(service, "service")
    direction = validate_direction(direction)
    config = db.get_config()
    depth = clamp_depth(depth, config)
    with db.connect() as conn:
        tree = service_queries.get_dependency_tree(conn, service, direction, depth, config.max_results)
    if tree is None:
        raise NotFoundError(f"No service named '{service}' found in the knowledge graph.")
    return {"service": service, "direction": direction, "depth": depth, "dependencies": tree}


def register(server):
    server.tool()(find_service)
    server.tool()(get_service_context)
    server.tool()(get_service_dependencies)
