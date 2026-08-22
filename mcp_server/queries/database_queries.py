"""Query logic backing get_database_impact.

There is no DATABASE_TABLE node type in the graph (see README "Known
limitations" — the indexer doesn't capture @Table names or column
mappings). Everything here is inferred from Repository classes and their
method names using Spring Data's own naming convention, and is always
returned with confidence ``inferred`` — never presented as a verified
schema fact.
"""
from ..db import fetch_all, row_to_dict
from ..models.evidence import prune_empty
from .api_queries import find_apis
from .flow_queries import _find_method_entry_point
from .common import (
    class_dependency_chain, classify_crud, find_exposing_method,
    get_class_methods, get_parent_class, inferred_table_name,
    is_repository_class, service_classes,
)

MAX_REPOS = 10
MAX_FUNCTIONS = 20


def _repos_via_controller(conn, controller_id, service):
    repos, _services_seen, _downstream = class_dependency_chain(conn, controller_id, service)
    return repos


def _resolve_repos(conn, service, entity, api, flow):
    """Returns (repo_rows, resolved_via) by priority: api > flow > entity > service."""
    if api:
        matches = find_apis(conn, query=api, service=service, limit=1)
        if not matches:
            return [], "api"
        method_row = find_exposing_method(conn, matches[0]["id"])
        if not method_row:
            return [], "api"
        controller_row = get_parent_class(conn, method_row["id"])
        if not controller_row:
            return [], "api"
        return _repos_via_controller(conn, controller_row["id"], matches[0].get("service")), "api"

    if flow:
        if not service:
            return [], "flow"
        matches = find_apis(conn, query=flow, service=service, limit=1)
        method_row = None
        if matches:
            method_row = find_exposing_method(conn, matches[0]["id"])
        else:
            fallback = _find_method_entry_point(conn, service, flow)
            method_row = fallback[0] if fallback else None
        if not method_row:
            return [], "flow"
        controller_row = get_parent_class(conn, method_row["id"])
        if not controller_row:
            return [], "flow"
        return _repos_via_controller(conn, controller_row["id"], service), "flow"

    if entity:
        candidates = [f"{entity}Repository", f"{entity}Repo", entity]
        sql = "SELECT * FROM nodes WHERE node_type IN ('CLASS','INTERFACE') AND name IN ({})".format(
            ",".join("?" for _ in candidates)
        )
        params = list(candidates)
        if service:
            sql += " AND service = ?"
            params.append(service)
        rows = [row_to_dict(r) for r in fetch_all(conn, sql, params)]
        repo_rows = [r for r in rows if is_repository_class(r)]
        if repo_rows:
            return repo_rows, "entity"

        # Reverse lookup: any repository whose inferred table name matches.
        classes = service_classes(conn, service) if service else _all_repository_candidates(conn)
        repo_rows = [
            c for c in classes
            if is_repository_class(c) and inferred_table_name(c["name"]).lower() == entity.lower()
        ]
        return repo_rows, "entity"

    if service:
        classes = service_classes(conn, service)
        repo_rows = [c for c in classes if is_repository_class(c)]
        return repo_rows[:MAX_REPOS], "service"

    return [], None


def _all_repository_candidates(conn, limit=500):
    rows = fetch_all(
        conn, "SELECT * FROM nodes WHERE node_type IN ('CLASS','INTERFACE') LIMIT ?", (limit,)
    )
    return [row_to_dict(r) for r in rows]


def get_database_impact(conn, service=None, entity=None, api=None, flow=None):
    repo_rows, resolved_via = _resolve_repos(conn, service, entity, api, flow)
    if not repo_rows:
        return None

    repo_rows = repo_rows[:MAX_REPOS]
    tables_read, tables_written, tables_updated, tables_deleted = set(), set(), set(), set()
    functions = []
    relationships = []

    for repo in repo_rows:
        table = inferred_table_name(repo["name"])
        relationships.append({
            "fact": f"{repo['name']} maps to table/entity '{table}'",
            "confidence": "inferred",
        })
        for method in get_class_methods(conn, repo["id"]):
            crud = classify_crud(method["name"])
            if not crud:
                continue
            {"read": tables_read, "write": tables_written, "update": tables_updated,
             "delete": tables_deleted}[crud].add(table)
            if len(functions) < MAX_FUNCTIONS:
                functions.append({
                    "repository": repo["name"],
                    "method": method["name"],
                    "operation": crud,
                    "confidence": "inferred",
                })

    return prune_empty({
        "resolved_via": resolved_via,
        "repositories": sorted({r["name"] for r in repo_rows}),
        "tables_read": sorted(tables_read),
        "tables_written": sorted(tables_written),
        "tables_updated": sorted(tables_updated),
        "tables_deleted": sorted(tables_deleted),
        "functions": functions,
        "relationships": relationships,
    })
