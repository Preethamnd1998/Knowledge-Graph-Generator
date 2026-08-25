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
    python example_queries.py jobs
    python example_queries.py job createPriceBookFromTempJob
    python example_queries.py kafka
    python example_queries.py topic POST_FINANCE_PRICE_BOOK
    python example_queries.py async
    python example_queries.py flow createPriceBookFromTempJob
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


# --- Spring Batch / Kafka / async flow queries (kg_indexer/parsers/spring_flow_parser.py) ---
#
# See that module's docstring for the confidence model. Every edge below
# carries metadata.confidence ("verified" | "inferred" | "unknown") in
# addition to whether target_id resolved — both are surfaced here rather
# than collapsed, so a caller can tell "definitely runs this job" apart
# from "references something we couldn't resolve".

FLOW_RELATIONS = ("DEFINES_BEAN", "HAS_STEP", "USES_TASKLET", "RUNS", "ASYNC_CALL",
                   "PUBLISHES", "CONSUMES", "WRITES", "READS")


def _find_typed_node(conn, name_or_qualified, node_type):
    row = conn.execute(
        "SELECT * FROM nodes WHERE node_type = ? AND (qualified_name = ? OR name = ?) LIMIT 1",
        (node_type, name_or_qualified, name_or_qualified),
    ).fetchone()
    return dict(row) if row else None


def _outgoing(conn, node_id, relations):
    placeholders = ",".join("?" for _ in relations)
    rows = conn.execute(
        f"SELECT e.relation, e.metadata AS edge_metadata, t.id AS target_id, t.node_type AS "
        f"target_type, t.name AS target_name, t.service AS target_service, e.target_name AS "
        f"unresolved_target FROM edges e LEFT JOIN nodes t ON t.id = e.target_id "
        f"WHERE e.source_id = ? AND e.relation IN ({placeholders})",
        [node_id, *relations],
    )
    return _rows_to_dicts(rows)


def _incoming(conn, node_id, relations):
    placeholders = ",".join("?" for _ in relations)
    rows = conn.execute(
        f"SELECT e.relation, e.metadata AS edge_metadata, s.id AS source_id, s.node_type AS "
        f"source_type, s.name AS source_name, s.service AS source_service FROM edges e "
        f"JOIN nodes s ON s.id = e.source_id WHERE e.target_id = ? AND e.relation IN ({placeholders})",
        [node_id, *relations],
    )
    return _rows_to_dicts(rows)


def find_jobs(conn, keyword=None, service=None):
    """All Spring Batch jobs (@Bean methods returning Job), optionally
    filtered by name keyword and/or service."""
    sql = "SELECT id, name, service, file_path, start_line, metadata FROM nodes WHERE node_type = 'BATCH_JOB'"
    params = []
    if keyword:
        sql += " AND name LIKE ?"
        params.append(f"%{keyword}%")
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " ORDER BY service, name"
    return _rows_to_dicts(conn.execute(sql, params))


def find_job_dependencies(conn, job_name):
    """A job's steps, each step's tasklet, and who RUNS this job — the
    full Spring Batch chain the graph can resolve for one job."""
    job = _find_typed_node(conn, job_name, "BATCH_JOB")
    if not job:
        return {"job": None}

    steps = []
    for edge in _outgoing(conn, job["id"], ("HAS_STEP",)):
        step = {"name": edge["target_name"] or edge["unresolved_target"], "resolved": edge["target_id"] is not None}
        if edge["target_id"] is not None:
            tasklets = [
                {"name": te["target_name"] or te["unresolved_target"], "resolved": te["target_id"] is not None}
                for te in _outgoing(conn, edge["target_id"], ("USES_TASKLET",))
            ]
            step["tasklets"] = tasklets
        steps.append(step)

    callers = [
        {"name": c["source_name"], "service": c["source_service"], "type": c["source_type"]}
        for c in _incoming(conn, job["id"], ("RUNS",))
    ]

    return {"job": job, "steps": steps, "run_by": callers}


def find_kafka_topics(conn, keyword=None):
    sql = "SELECT id, name, metadata FROM nodes WHERE node_type = 'KAFKA_TOPIC'"
    params = []
    if keyword:
        sql += " AND name LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY name"
    return _rows_to_dicts(conn.execute(sql, params))


def find_topic_producers(conn, topic_name):
    topic = _find_typed_node(conn, topic_name, "KAFKA_TOPIC")
    if not topic:
        return []
    return [
        {"name": p["source_name"], "service": p["source_service"], "type": p["source_type"]}
        for p in _incoming(conn, topic["id"], ("PUBLISHES",))
    ]


def find_topic_consumers(conn, topic_name):
    topic = _find_typed_node(conn, topic_name, "KAFKA_TOPIC")
    if not topic:
        return []
    return [
        {"name": c["source_name"], "service": c["source_service"], "type": c["source_type"]}
        for c in _incoming(conn, topic["id"], ("CONSUMES",))
    ]


def get_topic_flow(conn, topic_name):
    """Everything the graph knows about one Kafka topic: which
    services/classes publish it, and which consume it — this is how a
    cross-service flow (service A publishes -> topic -> service B
    consumes) is answered, since the topic node is never scoped to a
    single service."""
    topic = _find_typed_node(conn, topic_name, "KAFKA_TOPIC")
    if not topic:
        return {"topic": None}
    producers = find_topic_producers(conn, topic_name)
    consumers = find_topic_consumers(conn, topic_name)
    services = sorted({p["service"] for p in producers if p["service"]} |
                       {c["service"] for c in consumers if c["service"]})
    return {"topic": topic, "producers": producers, "consumers": consumers, "services": services}


def find_async_operations(conn, service=None):
    sql = "SELECT id, name, service, file_path, start_line, metadata FROM nodes WHERE node_type = 'ASYNC_OPERATION'"
    params = []
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " ORDER BY service, name"
    return _rows_to_dicts(conn.execute(sql, params))


def get_database_impact(conn, table_name):
    """Which classes/methods read/write a given (inferred) DB_TABLE."""
    table = _find_typed_node(conn, table_name, "DB_TABLE")
    if not table:
        return {"table": None}
    writers = _incoming(conn, table["id"], ("WRITES",))
    readers = _incoming(conn, table["id"], ("READS",))
    return {
        "table": table,
        "written_by": [{"name": w["source_name"], "service": w["source_service"]} for w in writers],
        "read_by": [{"name": r["source_name"], "service": r["source_service"]} for r in readers],
    }


def get_runtime_flow(conn, name):
    """General entry point for Phase 12: given a job/topic/class/method
    name, resolve it to whatever node type it actually is and return
    every flow-relation edge (both directions) touching it. Prefer the
    more specific find_job_dependencies/get_topic_flow when you already
    know which kind of node you have; this is for when you don't."""
    row = conn.execute(
        "SELECT * FROM nodes WHERE qualified_name = ? OR name = ? LIMIT 1", (name, name)
    ).fetchone()
    if not row:
        return {"node": None}
    node = dict(row)
    return {
        "node": node,
        "outgoing": _outgoing(conn, node["id"], FLOW_RELATIONS),
        "incoming": _incoming(conn, node["id"], FLOW_RELATIONS),
    }


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

    p_jobs = sub.add_parser("jobs")
    p_jobs.add_argument("keyword", nargs="?")
    p_jobs.add_argument("--service")

    p_job = sub.add_parser("job")
    p_job.add_argument("name")

    p_kafka = sub.add_parser("kafka")
    p_kafka.add_argument("keyword", nargs="?")

    p_topic = sub.add_parser("topic")
    p_topic.add_argument("name")

    p_async = sub.add_parser("async")
    p_async.add_argument("--service")

    p_table = sub.add_parser("table")
    p_table.add_argument("name")

    p_flow = sub.add_parser("flow")
    p_flow.add_argument("name")

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
    elif args.command == "jobs":
        _print_json(find_jobs(conn, keyword=args.keyword, service=args.service))
    elif args.command == "job":
        _print_json(find_job_dependencies(conn, args.name))
    elif args.command == "kafka":
        _print_json(find_kafka_topics(conn, keyword=args.keyword))
    elif args.command == "topic":
        _print_json(get_topic_flow(conn, args.name))
    elif args.command == "async":
        _print_json(find_async_operations(conn, service=args.service))
    elif args.command == "table":
        _print_json(get_database_impact(conn, args.name))
    elif args.command == "flow":
        _print_json(get_runtime_flow(conn, args.name))


if __name__ == "__main__":
    main()
