"""Query logic backing find_callers / find_dependencies — the generic
lookups get_service_dependencies specializes for the service case.

Honesty note: the graph has no CALLS relation (no parser tracks
method-body call sites — see README "Known limitations"), so
"find_callers" of a METHOD is answered at its *containing class*'s
DEPENDS_ON/EXTENDS/IMPLEMENTS in-edges (who holds/extends this class),
not real call-site tracking. That's the strongest honest signal the
graph has for "what uses this".
"""
from .common import (
    DEPENDENCY_RELATIONS, find_node_exact, find_nodes_fuzzy, get_parent_class, traverse,
)
from .service_queries import get_dependency_tree, service_exists


def _resolve_target(conn, target, service=None):
    """Returns ("service", name) or ("node", node_dict) or (None, None)."""
    if service_exists(conn, target):
        return "service", target

    node = find_node_exact(conn, target)
    if not node and service:
        matches = find_nodes_fuzzy(conn, target, service=service, limit=1)
        node = matches[0] if matches else None
    if not node:
        matches = find_nodes_fuzzy(conn, target, limit=1)
        node = matches[0] if matches else None
    if not node:
        return None, None

    if node["node_type"] == "METHOD":
        parent = get_parent_class(conn, node["id"])
        if parent:
            return "node", parent

    return "node", node


def find_dependencies(conn, target, direction="outgoing", depth=1, service=None, max_results=10):
    kind, resolved = _resolve_target(conn, target, service)
    if kind is None:
        return None
    if kind == "service":
        tree = get_dependency_tree(conn, resolved, direction, depth, max_results)
        return {"target": target, "target_type": "service", "results": tree or []}

    results = traverse(conn, resolved["id"], direction=direction, relations=DEPENDENCY_RELATIONS,
                        max_depth=depth, max_nodes=max_results)
    return {
        "target": resolved["name"],
        "target_type": resolved["node_type"],
        "service": resolved.get("service"),
        "results": results,
    }


def find_callers(conn, target, depth=1, service=None, max_results=10):
    return find_dependencies(conn, target, direction="incoming", depth=depth,
                              service=service, max_results=max_results)
