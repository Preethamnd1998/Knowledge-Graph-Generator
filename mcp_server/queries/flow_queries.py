"""Query logic backing get_business_flow.

A "flow" isn't a distinct thing the indexer models — there's no
BUSINESS_FLOW node type. This derives one on demand: find the
controller method whose name/endpoint best matches the requested flow
keyword, then reuse the same controller -> service -> repository walk
get_api_context uses, reshaped as an ordered chain.
"""
from ..db import fetch_all, row_to_dict
from ..models.evidence import prune_empty
from .api_queries import find_apis, find_ui_callers
from .common import (
    class_dependency_chain, find_exposing_method, get_parent_class,
    inferred_table_name, outgoing_edges, parse_metadata,
)

# Backend->backend + Kafka relations a controller method can carry directly
# (see spring_flow_parser.py / java_parser.py) — folded into the flow chain
# so "UI -> gateway -> backend -> Kafka/HTTP -> backend" is one call.
_DOWNSTREAM_CALL_RELATIONS = (
    "FEIGN_CALL", "WEBCLIENT_CALL", "RESTTEMPLATE_CALL", "PUBLISHES", "CONSUMES",
)


def _find_method_entry_point(conn, service, flow_keyword, limit=1):
    """Fallback when no REST endpoint matches: search METHOD nodes owned
    by this service directly (an internal flow with no HTTP entry point)."""
    tokens = [t.lower() for t in flow_keyword.split() if t]
    rows = fetch_all(
        conn,
        "SELECT m.* FROM nodes m WHERE m.service = ? AND m.node_type = 'METHOD'",
        (service,),
    )
    scored = []
    for row in rows:
        row = row_to_dict(row)
        haystack = row["name"].lower()
        score = sum(1 for t in tokens if t in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda pair: -pair[0])
    return [r for _, r in scored[:limit]]


def get_business_flow(conn, service, flow_keyword):
    entry_endpoint = None
    matches = find_apis(conn, query=flow_keyword, service=service, limit=1)
    controller_row = method_row = None

    if matches:
        entry_endpoint = matches[0]
        method_row = find_exposing_method(conn, entry_endpoint["id"])
        if method_row:
            controller_row = get_parent_class(conn, method_row["id"])
    else:
        method_matches = _find_method_entry_point(conn, service, flow_keyword)
        if method_matches:
            method_row = method_matches[0]
            controller_row = get_parent_class(conn, method_row["id"])

    if not method_row or not controller_row:
        return None

    repos, services_seen, downstream = class_dependency_chain(conn, controller_row["id"], service)
    repo_names = sorted({r["name"] for r in repos})
    tables = sorted({inferred_table_name(n) for n in repo_names})

    events = [r["name"] for r in fetch_all(
        conn,
        "SELECT DISTINCT name FROM nodes WHERE service = ? AND node_type IN "
        "('CLASS','INTERFACE') AND name LIKE '%Event' ORDER BY name LIMIT 10",
        (service,),
    )]

    ui_callers = find_ui_callers(conn, entry_endpoint["id"], limit=5) if entry_endpoint else []

    downstream_calls = [
        {
            "relation": e["relation"],
            "target": e.get("resolved_name") or e.get("target_name"),
            "target_service": e.get("resolved_service"),
            "confidence": "resolved" if e["target_id"] is not None else "unresolved",
        }
        for e in outgoing_edges(conn, method_row["id"], relations=_DOWNSTREAM_CALL_RELATIONS)
    ]

    chain = []
    for uc in ui_callers:
        chain.append({"step": uc["constant"], "role": "ui_caller", "detail": uc["ui_project"]})
    chain.append({"step": controller_row["name"], "role": "controller"})
    chain.append({"step": method_row["name"], "role": "method"})
    for svc_class in sorted(set(services_seen)):
        chain.append({"step": svc_class, "role": "service"})
    for repo in repo_names:
        chain.append({"step": repo, "role": "repository"})
    for other in sorted(downstream):
        chain.append({"step": other, "role": "downstream_service"})
    for dc in downstream_calls:
        chain.append({"step": dc["target"], "role": dc["relation"].lower(), "detail": dc["target_service"]})
    if events:
        chain.append({"step": events[0], "role": "event"})

    return prune_empty({
        "service": service,
        "flow": flow_keyword,
        "entry_api": (
            {"http_method": parse_metadata(entry_endpoint).get("http_method"),
             "path": parse_metadata(entry_endpoint).get("path")}
            if entry_endpoint else None
        ),
        "ui_callers": ui_callers,
        "chain": chain,
        "involved_services": sorted({service, *downstream, *(dc["target_service"] for dc in downstream_calls if dc["target_service"])}),
        "classes": sorted({controller_row["name"], *services_seen, *repo_names}),
        "database_tables": tables,
        "events": events,
        "external_calls": sorted(downstream),
        "downstream_calls": downstream_calls,
    })
