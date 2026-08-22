"""Shared node/edge lookups, classification heuristics, and the generic
depth-limited traversal every dependency-style tool builds on.

Confidence taxonomy used throughout the query layer (see also
models/evidence.py):

- ``verified``  — a real graph edge whose target resolved to a known
  node, or a fact read directly from a parsed annotation/decorator.
- ``inferred``  — derived here via a documented naming/structural
  heuristic (e.g. guessing a table name from a Repository class name),
  not something the indexer itself asserted.
- ``unknown``   — a real edge exists (the source really does reference
  this name) but it never resolved to a specific node — usually an
  external library type. We know *that* it's referenced, not *what*
  it precisely is.
"""
import json

from ..db import fetch_all, fetch_one, row_to_dict

CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}
SERVICE_ANNOTATIONS = {"Service"}
REPOSITORY_ANNOTATIONS = {"Repository"}
ENTITY_ANNOTATIONS = {"Entity", "Document"}
REPOSITORY_BASE_INTERFACES = {
    "JpaRepository", "CrudRepository", "MongoRepository", "PagingAndSortingRepository",
    "ReactiveCrudRepository", "ReactiveMongoRepository",
}

# Dependency-shaped relations — the structural ones (CONTAINS/DECLARES) are
# "X holds Y", not "X depends on Y", so traversal tools exclude them by default.
DEPENDENCY_RELATIONS = ("DEPENDS_ON", "EXTENDS", "IMPLEMENTS", "IMPORTS")


def parse_metadata(row):
    raw = row["metadata"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def node_annotations(row):
    return set(parse_metadata(row).get("annotations", []) or [])


def is_controller(row):
    return bool(node_annotations(row) & CONTROLLER_ANNOTATIONS)


def is_service_class(row):
    return bool(node_annotations(row) & SERVICE_ANNOTATIONS)


def is_repository_class(row, extends_targets=()):
    if node_annotations(row) & REPOSITORY_ANNOTATIONS:
        return True
    if set(extends_targets) & REPOSITORY_BASE_INTERFACES:
        return True
    name = row["name"] or ""
    return name.endswith("Repository") or name.endswith("Repo")


def is_entity_class(row):
    return bool(node_annotations(row) & ENTITY_ANNOTATIONS)


def inferred_table_name(repository_class_name):
    """Best-effort table/entity name guess from a Repository class name —
    confidence ``inferred``, never presented as a verified fact."""
    for suffix in ("Repository", "Repo"):
        if repository_class_name.endswith(suffix):
            return repository_class_name[: -len(suffix)]
    return repository_class_name


def classify_crud(method_name):
    """Heuristic CRUD classification from a repository method's name
    (Spring Data query-derivation naming convention). Confidence ``inferred``."""
    name = (method_name or "").lower()
    if name.startswith(("find", "get", "read", "exists", "count", "query")):
        return "read"
    if name.startswith(("save", "insert", "create", "persist", "add")):
        return "write"
    if name.startswith(("update", "modify", "set")):
        return "update"
    if name.startswith(("delete", "remove", "drop")):
        return "delete"
    return None


def find_node_exact(conn, name):
    """Exact match on qualified_name first, then name (services/APIs are
    usually looked up by their short name)."""
    row = fetch_one(conn, "SELECT * FROM nodes WHERE qualified_name = ? LIMIT 1", (name,))
    if row:
        return row_to_dict(row)
    row = fetch_one(conn, "SELECT * FROM nodes WHERE name = ? LIMIT 1", (name,))
    return row_to_dict(row)


def find_nodes_fuzzy(conn, keyword, node_types=None, service=None, limit=10):
    sql = "SELECT * FROM nodes WHERE (name LIKE ? OR qualified_name LIKE ?)"
    params = [f"%{keyword}%", f"%{keyword}%"]
    if node_types:
        placeholders = ",".join("?" for _ in node_types)
        sql += f" AND node_type IN ({placeholders})"
        params.extend(node_types)
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " ORDER BY name LIMIT ?"
    params.append(limit)
    return [row_to_dict(r) for r in fetch_all(conn, sql, params)]


def list_distinct_services(conn, keyword=None, limit=10):
    sql = "SELECT DISTINCT service, project_type FROM nodes WHERE service IS NOT NULL"
    params = []
    if keyword:
        sql += " AND service LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY service LIMIT ?"
    params.append(limit)
    return [row_to_dict(r) for r in fetch_all(conn, sql, params)]


def service_node_counts(conn, service):
    rows = fetch_all(
        conn, "SELECT node_type, COUNT(*) AS n FROM nodes WHERE service = ? GROUP BY node_type",
        (service,),
    )
    return {r["node_type"]: r["n"] for r in rows}


def service_classes(conn, service, node_types=("CLASS", "INTERFACE")):
    placeholders = ",".join("?" for _ in node_types)
    rows = fetch_all(
        conn, f"SELECT * FROM nodes WHERE service = ? AND node_type IN ({placeholders})",
        (service, *node_types),
    )
    return [row_to_dict(r) for r in rows]


def outgoing_edges(conn, node_id, relations=None):
    sql = ("SELECT e.*, t.name AS resolved_name, t.service AS resolved_service, "
           "t.node_type AS resolved_type FROM edges e "
           "LEFT JOIN nodes t ON t.id = e.target_id WHERE e.source_id = ?")
    params = [node_id]
    if relations:
        placeholders = ",".join("?" for _ in relations)
        sql += f" AND e.relation IN ({placeholders})"
        params.extend(relations)
    return [row_to_dict(r) for r in fetch_all(conn, sql, params)]


def incoming_edges(conn, node_id, relations=None):
    sql = ("SELECT e.*, s.name AS source_name, s.service AS source_service, "
           "s.node_type AS source_type FROM edges e "
           "JOIN nodes s ON s.id = e.source_id WHERE e.target_id = ?")
    params = [node_id]
    if relations:
        placeholders = ",".join("?" for _ in relations)
        sql += f" AND e.relation IN ({placeholders})"
        params.extend(relations)
    return [row_to_dict(r) for r in fetch_all(conn, sql, params)]


def get_class_methods(conn, class_id):
    rows = fetch_all(
        conn,
        "SELECT t.* FROM edges e JOIN nodes t ON t.id = e.target_id "
        "WHERE e.source_id = ? AND e.relation = 'CONTAINS' AND t.node_type = 'METHOD'",
        (class_id,),
    )
    return [row_to_dict(r) for r in rows]


def get_parent_class(conn, method_id):
    row = fetch_one(
        conn,
        "SELECT s.* FROM edges e JOIN nodes s ON s.id = e.source_id "
        "WHERE e.target_id = ? AND e.relation = 'CONTAINS'",
        (method_id,),
    )
    return row_to_dict(row)


def find_exposing_method(conn, endpoint_id):
    """The METHOD node whose @*Mapping produced this ENDPOINT (i.e. the
    controller handler), if the graph still has it resolved."""
    row = fetch_one(
        conn,
        "SELECT s.* FROM edges e JOIN nodes s ON s.id = e.source_id "
        "WHERE e.target_id = ? AND e.relation = 'EXPOSES' LIMIT 1",
        (endpoint_id,),
    )
    return row_to_dict(row)


MAX_CHAIN_DEPTH = 3  # controller -> service -> service -> repository is a realistic max depth


def class_dependency_chain(conn, class_id, own_service):
    """Walk DEPENDS_ON from a class up to MAX_CHAIN_DEPTH hops deep
    (controller -> service [-> service] -> repository is the common
    Spring shape), classifying what it finds. Returns
    (repository_class_rows, service_class_names, downstream_services) —
    repos as full node dicts (callers need `.id` to inspect methods for
    CRUD classification, not just the name) — used by get_api_context,
    get_business_flow, and get_database_impact so "what does this
    controller ultimately touch" is computed identically everywhere.
    Bounded depth + a seen-repo set keep this cheap and cycle-safe even
    if two service beans depend on each other.
    """
    repos, services_seen, downstream = [], [], set()
    seen_repo_ids = set()
    seen_class_ids = {class_id}  # cycle protection: A depends_on B depends_on A must terminate

    def _visit(node_id, depth):
        if depth > MAX_CHAIN_DEPTH:
            return
        for edge in outgoing_edges(conn, node_id, relations=["DEPENDS_ON"]):
            if edge.get("resolved_service") and edge["resolved_service"] != own_service:
                downstream.add(edge["resolved_service"])
            if edge["target_id"] is None:
                continue
            if edge["target_id"] in seen_class_ids:
                continue
            target = row_to_dict(fetch_one(conn, "SELECT * FROM nodes WHERE id = ?", (edge["target_id"],)))
            if not target:
                continue
            if is_repository_class(target):
                if target["id"] not in seen_repo_ids:
                    seen_repo_ids.add(target["id"])
                    repos.append(target)
            elif is_service_class(target):
                seen_class_ids.add(target["id"])
                services_seen.append(target["name"])
                _visit(target["id"], depth + 1)

    _visit(class_id, 1)
    return repos, services_seen, downstream


def traverse(conn, start_id, direction="outgoing", relations=DEPENDENCY_RELATIONS,
             max_depth=1, max_nodes=50):
    """Depth-limited BFS with cycle protection. Never expands past
    max_depth or collects more than max_nodes results, regardless of how
    large the underlying graph is (Rules 4/5/6: no unrelated
    relationships, small default limits, depth always bounded).

    Returns a list of dicts: {node, relation, depth, confidence}.
    """
    visited = {start_id}
    frontier = [start_id]
    results = []

    for depth in range(1, max_depth + 1):
        if not frontier or len(results) >= max_nodes:
            break
        next_frontier = []
        for node_id in frontier:
            if direction in ("outgoing", "both"):
                for edge in outgoing_edges(conn, node_id, relations):
                    if len(results) >= max_nodes:
                        break
                    target_id = edge["target_id"]
                    confidence = "verified" if target_id is not None else "unknown"
                    name = edge["resolved_name"] or edge["target_name"]
                    if target_id is not None:
                        if target_id in visited:
                            continue
                        visited.add(target_id)
                        next_frontier.append(target_id)
                    results.append({
                        "name": name,
                        "service": edge.get("resolved_service"),
                        "node_type": edge.get("resolved_type"),
                        "relation": edge["relation"],
                        "direction": "outgoing",
                        "depth": depth,
                        "confidence": confidence,
                    })
            if direction in ("incoming", "both"):
                for edge in incoming_edges(conn, node_id, relations):
                    if len(results) >= max_nodes:
                        break
                    source_id = edge["source_id"]
                    if source_id in visited:
                        continue
                    visited.add(source_id)
                    next_frontier.append(source_id)
                    results.append({
                        "name": edge.get("source_name"),
                        "service": edge.get("source_service"),
                        "node_type": edge.get("source_type"),
                        "relation": edge["relation"],
                        "direction": "incoming",
                        "depth": depth,
                        "confidence": "verified",
                    })
        frontier = next_frontier

    return results
