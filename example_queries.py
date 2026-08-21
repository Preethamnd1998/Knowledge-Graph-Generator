#!/usr/bin/env python3
"""Example queries against a knowledge_graph.db produced by indexer.py.

Importable for programmatic/agent access:
    from example_queries import connect, list_endpoints, get_dependencies
    conn = connect("output/knowledge_graph.db")
    for row in list_endpoints(conn, service="core-service"):
        print(row)

Also runnable directly as a CLI:
    python example_queries.py summary
    python example_queries.py endpoints --service core-service
    python example_queries.py search PatientController
    python example_queries.py deps PatientController
    python example_queries.py dependents PatientRepository
    python example_queries.py hooks useEffect
    python example_queries.py sql "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"
"""
import argparse
import json
import os
import sqlite3
import sys


def connect(db_path):
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"No such database: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows):
    return [dict(r) for r in rows]


def graph_summary(conn):
    nodes = _rows_to_dicts(conn.execute(
        "SELECT node_type, COUNT(*) AS count FROM nodes GROUP BY node_type ORDER BY count DESC"
    ))
    edges = _rows_to_dicts(conn.execute(
        "SELECT relation, COUNT(*) AS count FROM edges GROUP BY relation ORDER BY count DESC"
    ))
    services = _rows_to_dicts(conn.execute(
        "SELECT service, project_type, COUNT(*) AS node_count FROM nodes "
        "WHERE service IS NOT NULL GROUP BY service, project_type ORDER BY service"
    ))
    return {"nodes_by_type": nodes, "edges_by_relation": edges, "services": services}


def list_endpoints(conn, service=None):
    sql = ("SELECT service, name, file_path, start_line, metadata FROM nodes "
           "WHERE node_type = 'ENDPOINT'")
    params = []
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " ORDER BY service, name"
    return _rows_to_dicts(conn.execute(sql, params))


def search_nodes(conn, keyword, node_type=None):
    sql = ("SELECT node_type, name, qualified_name, service, file_path, start_line "
           "FROM nodes WHERE name LIKE ?")
    params = [f"%{keyword}%"]
    if node_type:
        sql += " AND node_type = ?"
        params.append(node_type)
    sql += " ORDER BY service, name LIMIT 200"
    return _rows_to_dicts(conn.execute(sql, params))


def _find_node(conn, name_or_qualified):
    row = conn.execute(
        "SELECT * FROM nodes WHERE qualified_name = ? OR name = ? LIMIT 1",
        (name_or_qualified, name_or_qualified),
    ).fetchone()
    return dict(row) if row else None


def get_dependencies(conn, name_or_qualified, relations=("DEPENDS_ON", "EXTENDS", "IMPLEMENTS")):
    """Outgoing edges from the given node — what it depends on."""
    node = _find_node(conn, name_or_qualified)
    if not node:
        return {"node": None, "dependencies": []}
    placeholders = ",".join("?" for _ in relations)
    rows = conn.execute(
        f"SELECT e.relation, e.target_name, e.metadata, n.qualified_name AS "
        f"resolved_qualified_name, n.service AS resolved_service FROM edges e "
        f"LEFT JOIN nodes n ON n.id = e.target_id "
        f"WHERE e.source_id = ? AND e.relation IN ({placeholders})",
        [node["id"], *relations],
    )
    return {"node": node, "dependencies": _rows_to_dicts(rows)}


def get_dependents(conn, name_or_qualified, relations=("DEPENDS_ON", "EXTENDS", "IMPLEMENTS")):
    """Incoming edges to the given node — what depends on it (resolved edges only)."""
    node = _find_node(conn, name_or_qualified)
    if not node:
        return {"node": None, "dependents": []}
    placeholders = ",".join("?" for _ in relations)
    rows = conn.execute(
        f"SELECT n.qualified_name AS source_qualified_name, n.service AS "
        f"source_service, e.relation FROM edges e "
        f"JOIN nodes n ON n.id = e.source_id "
        f"WHERE e.target_id = ? AND e.relation IN ({placeholders})",
        [node["id"], *relations],
    )
    return {"node": node, "dependents": _rows_to_dicts(rows)}


def components_using_hook(conn, hook_name):
    rows = conn.execute(
        "SELECT n.service, n.name, n.file_path, n.start_line FROM edges e "
        "JOIN nodes n ON n.id = e.source_id "
        "WHERE e.relation = 'USES_HOOK' AND e.target_name = ? ORDER BY n.service, n.name",
        (hook_name,),
    )
    return _rows_to_dicts(rows)


def cross_service_dependencies(conn):
    """Resolved edges whose source and target belong to different services —
    useful for spotting unwanted coupling between microservices/subprojects.
    """
    rows = conn.execute(
        "SELECT s.service AS from_service, s.name AS from_name, e.relation, "
        "t.service AS to_service, t.name AS to_name FROM edges e "
        "JOIN nodes s ON s.id = e.source_id "
        "JOIN nodes t ON t.id = e.target_id "
        "WHERE t.id IS NOT NULL AND s.service != t.service"
    )
    return _rows_to_dicts(rows)


def run_sql(conn, sql):
    if not sql.strip().lower().startswith("select"):
        raise ValueError("Only SELECT statements are allowed.")
    return _rows_to_dicts(conn.execute(sql))


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.path.join("output", "knowledge_graph.db"),
                         help="Path to the .db file (default: output/knowledge_graph.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary")

    p_endpoints = sub.add_parser("endpoints")
    p_endpoints.add_argument("--service")

    p_search = sub.add_parser("search")
    p_search.add_argument("keyword")
    p_search.add_argument("--type", dest="node_type")

    p_deps = sub.add_parser("deps")
    p_deps.add_argument("name")

    p_dependents = sub.add_parser("dependents")
    p_dependents.add_argument("name")

    p_hooks = sub.add_parser("hooks")
    p_hooks.add_argument("hook_name")

    sub.add_parser("cross-service")

    p_sql = sub.add_parser("sql")
    p_sql.add_argument("query")

    args = parser.parse_args()

    try:
        conn = connect(args.db)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    if args.command == "summary":
        _print_json(graph_summary(conn))
    elif args.command == "endpoints":
        _print_json(list_endpoints(conn, service=args.service))
    elif args.command == "search":
        _print_json(search_nodes(conn, args.keyword, node_type=args.node_type))
    elif args.command == "deps":
        _print_json(get_dependencies(conn, args.name))
    elif args.command == "dependents":
        _print_json(get_dependents(conn, args.name))
    elif args.command == "hooks":
        _print_json(components_using_hook(conn, args.hook_name))
    elif args.command == "cross-service":
        _print_json(cross_service_dependencies(conn))
    elif args.command == "sql":
        _print_json(run_sql(conn, args.query))


if __name__ == "__main__":
    main()
