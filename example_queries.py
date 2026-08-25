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
    python example_queries.py flow BillingApiConstant.BILLING_CONFIRM_PAYMENT
    python example_queries.py flow price_book --depth 3
    python example_queries.py flow POST_FINANCE_PRICE_BOOK --direction reverse
    python example_queries.py flow PriceBookUploadServiceImpl --format json
    python example_queries.py api-constants BILLING_CONFIRM_PAYMENT
    python example_queries.py ui-to-api "ipd/opd/confirm/payment"
    python example_queries.py api-callers core-service GET /patient/list
    python example_queries.py service-calls --service core-service
    python example_queries.py full-flow ipd-service POST /opd/confirm/payment
"""
import argparse
import json
import os
import sqlite3
import sys

from kg_indexer import flow_tracer


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


# --- UI <-> Backend API mapping (kg_indexer/db.py resolve_api_constant_edges
# / resolve_service_call_edges) ---
#
# API_CONSTANT (Angular/React `FooApiConstant.FIELD = "path"`) --CALLS_API-->
# ENDPOINT (Java @*Mapping) is the UI->backend edge; FILE --USES_API_CONSTANT-->
# API_CONSTANT is "who in the UI references this constant"; FEIGN_CALL /
# WEBCLIENT_CALL / RESTTEMPLATE_CALL (METHOD -> ENDPOINT) are backend->backend.
# All four resolve only within SINGLE_DATABASE=true (see AGENTS.md).

SERVICE_CALL_RELATIONS = ("FEIGN_CALL", "WEBCLIENT_CALL", "RESTTEMPLATE_CALL")


def find_api_constants(conn, keyword=None, service=None):
    """UI API_CONSTANT nodes (class.FIELD = "path"), optionally filtered by
    a keyword against the field/class name or the literal path, and/or the
    Angular sub-project/React app that declares them."""
    sql = "SELECT id, name, service, file_path, start_line, metadata FROM nodes WHERE node_type = 'API_CONSTANT'"
    params = []
    if keyword:
        sql += " AND (name LIKE ? OR json_extract(metadata, '$.raw_value') LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " ORDER BY service, name"
    return _rows_to_dicts(conn.execute(sql, params))


def find_backend_for_ui_constant(conn, name_or_path):
    """Answers "which backend endpoint does this UI API constant call?".
    `name_or_path` may be a `ClassName.FIELD` qualified/simple name, or the
    raw path string itself (e.g. "ipd/opd/confirm/payment"). Returns every
    matching API_CONSTANT (there can be more than one — the same class/field
    pair is sometimes duplicated across Angular libraries, see AGENTS.md)
    with its resolution status and the ENDPOINT(s) it CALLS_API to, if any.
    """
    rows = conn.execute(
        "SELECT id, qualified_name, name, service, file_path, metadata FROM nodes "
        "WHERE node_type = 'API_CONSTANT' AND (qualified_name LIKE ? OR name = ? OR "
        "json_extract(metadata, '$.normalized_path') = ?)",
        (f"%{name_or_path}", name_or_path, name_or_path.strip("/")),
    ).fetchall()
    results = []
    for row in rows:
        node = dict(row)
        edges = conn.execute(
            "SELECT e.metadata AS edge_metadata, n.service, n.name, n.qualified_name, n.metadata AS endpoint_metadata "
            "FROM edges e JOIN nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ? AND e.relation = 'CALLS_API'",
            (node["id"],),
        ).fetchall()
        results.append({"constant": node, "endpoints": _rows_to_dicts(edges)})
    return results


def find_ui_callers_of_endpoint(conn, service, http_method, path):
    """Answers "which UI screens/services call this backend endpoint?".
    `service`/`http_method`/`path` identify the ENDPOINT exactly as stored
    (see list_endpoints) — path normalized the same way java_parser.py
    stores it (leading '/', no trailing slash unless the mapping itself
    has one). Returns each calling API_CONSTANT plus the UI file(s) that
    reference it via USES_API_CONSTANT."""
    endpoint = conn.execute(
        "SELECT id FROM nodes WHERE node_type = 'ENDPOINT' AND service = ? AND "
        "json_extract(metadata, '$.http_method') = ? AND json_extract(metadata, '$.path') = ?",
        (service, http_method.upper(), path),
    ).fetchone()
    if not endpoint:
        return {"endpoint": None, "callers": []}
    constants = conn.execute(
        "SELECT s.id, s.name, s.service, s.qualified_name FROM edges e "
        "JOIN nodes s ON s.id = e.source_id WHERE e.target_id = ? AND e.relation = 'CALLS_API'",
        (endpoint["id"],),
    ).fetchall()
    callers = []
    for c in constants:
        files = conn.execute(
            "SELECT n.service, n.file_path FROM edges e JOIN nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ? AND e.relation = 'USES_API_CONSTANT'",
            (c["id"],),
        ).fetchall()
        callers.append({"constant": dict(c), "referenced_from": _rows_to_dicts(files)})
    return {"endpoint": {"service": service, "http_method": http_method, "path": path}, "callers": callers}


def find_service_calls(conn, service=None, relations=SERVICE_CALL_RELATIONS):
    """Backend->backend calls (Feign/WebClient/RestTemplate), optionally
    filtered to those originating in one service. Each row carries its own
    confidence/resolution — see spring_flow_parser.py / java_parser.py
    module docs for what 'inferred' vs 'unknown' means here."""
    placeholders = ",".join("?" for _ in relations)
    sql = (
        f"SELECT s.service AS from_service, s.name AS from_method, e.relation, "
        f"e.metadata, t.service AS to_service, t.name AS to_endpoint FROM edges e "
        f"JOIN nodes s ON s.id = e.source_id LEFT JOIN nodes t ON t.id = e.target_id "
        f"WHERE e.relation IN ({placeholders})"
    )
    params = list(relations)
    if service:
        sql += " AND s.service = ?"
        params.append(service)
    sql += " ORDER BY from_service, from_method"
    return _rows_to_dicts(conn.execute(sql, params))


def get_full_flow(conn, service, http_method, path):
    """UI -> gateway -> backend -> Kafka/HTTP -> backend, assembled around
    one ENDPOINT: its UI callers (find_ui_callers_of_endpoint), the handler
    method's outgoing backend->backend calls, and Kafka publish/consume —
    everything this graph can resolve about one entry point in one call."""
    result = find_ui_callers_of_endpoint(conn, service, http_method, path)
    endpoint_row = conn.execute(
        "SELECT id FROM nodes WHERE node_type = 'ENDPOINT' AND service = ? AND "
        "json_extract(metadata, '$.http_method') = ? AND json_extract(metadata, '$.path') = ?",
        (service, http_method.upper(), path),
    ).fetchone()
    if not endpoint_row:
        return {"endpoint": None}
    method_row = conn.execute(
        "SELECT s.id, s.name FROM edges e JOIN nodes s ON s.id = e.source_id "
        "WHERE e.target_id = ? AND e.relation = 'EXPOSES'", (endpoint_row["id"],),
    ).fetchone()
    downstream = []
    if method_row:
        downstream = _outgoing(conn, method_row["id"], SERVICE_CALL_RELATIONS + ("PUBLISHES", "CONSUMES"))
    return {
        "ui_callers": result["callers"],
        "endpoint": {"service": service, "http_method": http_method, "path": path},
        "handler": dict(method_row) if method_row else None,
        "downstream": downstream,
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


# --- Business flow tracer (kg_indexer/flow_tracer.py) ---
#
# Multi-hop traversal on top of the graph's real edges, in either narrative
# direction, with an explicit VERIFIED/INFERRED/UNRESOLVED confidence and
# (for VERIFIED hops) source evidence on every hop — see that module's
# docstring for the full design and AGENTS.md for the write-up. Distinct
# from get_runtime_flow() above (a single-hop, flow-relations-only lookup,
# kept as-is for backward compatibility) — this is the richer replacement
# behind the `flow` CLI subcommand.

def trace_business_flow(conn, target, direction="both", max_depth=None):
    return flow_tracer.trace(conn, target, direction=direction,
                              max_depth=max_depth or flow_tracer.DEFAULT_MAX_DEPTH)


def _fmt_node_label(node_ref):
    if not node_ref.get("resolved"):
        return f"{node_ref.get('name') or '?'} [UNRESOLVED]"
    label = node_ref.get("qualified_name") or node_ref.get("name") or "?"
    node_type = node_ref.get("node_type") or "?"
    svc = f" @ {node_ref['service']}" if node_ref.get("service") else ""
    return f"{label} ({node_type}{svc})"


def _build_flow_tree(paths):
    root = {}
    for path in paths:
        node = root
        for hop in path:
            key = (hop["relation"], hop["target"].get("id"), hop["target"].get("name"))
            node = node.setdefault(key, {"hop": hop, "children": {}})["children"]
    return root


def _print_flow_tree(tree, indent, out):
    prefix = "  " * indent
    for entry in tree.values():
        hop = entry["hop"]
        cycle = " (cycle — stops here)" if hop.get("cycle") else ""
        out.append(f"{prefix}|-- {hop['relation']} [{hop['confidence']}] "
                    f"({hop['path_type']}, {hop['direction']}){cycle}")
        out.append(f"{prefix}      -> {_fmt_node_label(hop['target'])}")
        if hop.get("reason"):
            out.append(f"{prefix}      reason: {hop['reason']}")
        if hop.get("evidence"):
            ev = hop["evidence"]
            loc = ev.get("file") or ""
            if ev.get("line_start") is not None:
                loc += f":{ev['line_start']}"
            sym = ev.get("symbol") or ""
            out.append(f"{prefix}      evidence: {loc}" + (f" ({sym})" if sym else ""))
        _print_flow_tree(entry["children"], indent + 1, out)


def format_flow_text(result):
    lines = [f"Target: {result['target']}"]
    if not result.get("resolved"):
        lines.append(result.get("message", "Not found."))
        return "\n".join(lines)

    lines.append(f"Direction: {result['direction']}  |  Max depth: {result['max_depth']}")
    if result.get("ambiguous"):
        note = " (more candidates exist — refine the target to narrow this down)" \
            if result.get("candidates_truncated") else ""
        lines.append(f"Ambiguous target: {len(result['results'])} candidate(s) matched{note}")
    cs = result.get("confidence_summary", {})
    lines.append(f"Confidence summary: VERIFIED={cs.get('VERIFIED', 0)}  "
                 f"INFERRED={cs.get('INFERRED', 0)}  UNRESOLVED={cs.get('UNRESOLVED', 0)}")

    for i, root in enumerate(result["results"], 1):
        label = _fmt_node_label(root["start_node"])
        lines.append("")
        lines.append(f"=== [{i}/{len(result['results'])}] {label} ===")
        if root["forward_paths"]:
            lines.append("-- forward (downstream effects) --")
            lines.append(label)
            _print_flow_tree(_build_flow_tree(root["forward_paths"]), 1, lines)
        if root["reverse_paths"]:
            lines.append("-- reverse (upstream callers) --")
            lines.append(label)
            _print_flow_tree(_build_flow_tree(root["reverse_paths"]), 1, lines)
        if not root["forward_paths"] and not root["reverse_paths"]:
            lines.append("(no connected flow found in the requested direction — the graph "
                         "has no edges leaving/entering this node of the relevant kinds)")
    return "\n".join(lines)


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
    p_flow.add_argument("--depth", type=int, default=flow_tracer.DEFAULT_MAX_DEPTH,
                         help=f"max hops per path (default {flow_tracer.DEFAULT_MAX_DEPTH}, "
                              f"capped at {flow_tracer.MAX_ALLOWED_DEPTH})")
    p_flow.add_argument("--direction", choices=("forward", "reverse", "both"), default="both")
    p_flow.add_argument("--format", choices=("text", "json"), default="text")

    p_api_constants = sub.add_parser("api-constants")
    p_api_constants.add_argument("keyword", nargs="?")
    p_api_constants.add_argument("--service")

    p_ui_to_api = sub.add_parser("ui-to-api")
    p_ui_to_api.add_argument("name_or_path")

    p_api_callers = sub.add_parser("api-callers")
    p_api_callers.add_argument("service")
    p_api_callers.add_argument("http_method")
    p_api_callers.add_argument("path")

    p_service_calls = sub.add_parser("service-calls")
    p_service_calls.add_argument("--service")

    p_full_flow = sub.add_parser("full-flow")
    p_full_flow.add_argument("service")
    p_full_flow.add_argument("http_method")
    p_full_flow.add_argument("path")

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
        result = trace_business_flow(conn, args.name, direction=args.direction, max_depth=args.depth)
        if args.format == "json":
            _print_json(result)
        else:
            print(format_flow_text(result))
    elif args.command == "api-constants":
        _print_json(find_api_constants(conn, keyword=args.keyword, service=args.service))
    elif args.command == "ui-to-api":
        _print_json(find_backend_for_ui_constant(conn, args.name_or_path))
    elif args.command == "api-callers":
        _print_json(find_ui_callers_of_endpoint(conn, args.service, args.http_method, args.path))
    elif args.command == "service-calls":
        _print_json(find_service_calls(conn, service=args.service))
    elif args.command == "full-flow":
        _print_json(get_full_flow(conn, args.service, args.http_method, args.path))


if __name__ == "__main__":
    main()
