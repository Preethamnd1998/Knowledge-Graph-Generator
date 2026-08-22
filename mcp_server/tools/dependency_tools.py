"""find_callers, find_dependencies — generic lookups for a class, method,
API, service, or entity. See queries/dependency_queries.py for the
important honesty note on what "callers" means without a CALLS relation.
"""
from .. import db
from ..errors import NotFoundError
from ..limits import clamp_depth, clamp_limit, require_non_empty, validate_direction
from ..logging_setup import tool_boundary
from ..queries import dependency_queries


@tool_boundary("find_callers")
def find_callers(target: str, depth: int = 1) -> dict:
    """What calls/depends on a class, method, API's controller, or
    service — direct callers by default, or up to `depth` hops. A METHOD
    target resolves to its containing class (the graph has no per-call-site
    tracking; class-level DEPENDS_ON/EXTENDS/IMPLEMENTS is the strongest
    real signal available)."""
    target = require_non_empty(target, "target")
    config = db.get_config()
    depth = clamp_depth(depth, config)
    with db.connect() as conn:
        result = dependency_queries.find_callers(conn, target, depth=depth, max_results=config.max_results)
    if result is None:
        raise NotFoundError(f"'{target}' was not found in the knowledge graph.")
    return result


@tool_boundary("find_dependencies")
def find_dependencies(target: str, direction: str = "outgoing", depth: int = 1) -> dict:
    """Generic dependency lookup for a class, method, API's controller,
    service, or entity — works the same way regardless of node type so
    the caller never needs to know the underlying graph shape."""
    target = require_non_empty(target, "target")
    direction = validate_direction(direction)
    config = db.get_config()
    depth = clamp_depth(depth, config)
    with db.connect() as conn:
        result = dependency_queries.find_dependencies(conn, target, direction=direction,
                                                        depth=depth, max_results=config.max_results)
    if result is None:
        raise NotFoundError(f"'{target}' was not found in the knowledge graph.")
    return result


def register(server):
    server.tool()(find_callers)
    server.tool()(find_dependencies)
