"""Query logic backing the saga_* tools.

A "saga" is the whole path of one action: UI trigger -> endpoint -> backend
methods -> (Feign / WebClient / Kafka hops into other services) -> DB write.
These queries return the *graph facts* each hop needs; walking method-to-method
calls is Code Cortex's job (this graph has no CALLS edges), and composing both
is unified-trace's `saga` tool. Every unresolved link is returned WITH its
reason rather than dropped or guessed — a saga with a named gap is useful, a
saga with a silently invented hop is not.
"""
import re
from urllib.parse import urlparse

from ..db import fetch_all, fetch_one, row_to_dict
from .common import find_exposing_method, get_parent_class, parse_metadata

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
EFFECT_RELATIONS = ("WRITES", "READS", "PUBLISHES", "CONSUMES", "FEIGN_CALL", "WEBCLIENT_CALL",
                    "RESTTEMPLATE_CALL", "ASYNC_CALL", "RUNS")
_CALL_RELATIONS = ("FEIGN_CALL", "WEBCLIENT_CALL", "RESTTEMPLATE_CALL")
_TEMPLATE_VAR_RE = re.compile(r"\{[^/}]*\}")


# --------------------------------------------------------------------------
# Entry resolution: URL / "METHOD /path" / gateway-style path -> endpoints
# --------------------------------------------------------------------------

def gateway_prefix_map(conn):
    """{gateway prefix (lowercase) -> service name}, read from SERVICE nodes —
    the same map the indexer used to resolve Feign/WebClient targets."""
    out = {}
    for r in fetch_all(conn, "SELECT name, metadata FROM nodes WHERE node_type = 'SERVICE'"):
        prefix = parse_metadata(row_to_dict(r)).get("gateway_prefix")
        if prefix:
            out[prefix.lower()] = r["name"]
    return out


def normalize_target(conn, target, method=None, service=None):
    """Any of: 'https://host/api-gateway/ipd-clinical/settings/6a4b..',
    'PUT /settings/{id}', '/settings/x', 'ipd-clinical/settings/x'
    -> (service|None, http_method|None, path). An explicit `service` /
    `method` argument always wins over what the string implies."""
    s = (target or "").strip()
    m = re.match(r"^(GET|POST|PUT|DELETE|PATCH)\s+(.*)$", s, re.I)
    if m:
        method, s = method or m.group(1).upper(), m.group(2).strip()
    if "://" in s or s.startswith("//"):
        s = urlparse(s if "://" in s else "http:" + s).path
    s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
    segments = [seg for seg in s.split("/") if seg]
    if segments and segments[0].lower() == "api-gateway":
        segments = segments[1:]
    prefixes = gateway_prefix_map(conn)
    if not service and segments and segments[0].lower() in prefixes:
        service = prefixes[segments[0].lower()]
        segments = segments[1:]
    return service, (method.upper() if method else None), "/" + "/".join(segments)


def _template_regex(template_path):
    parts = _TEMPLATE_VAR_RE.split(template_path)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "/?$")


def find_entry_endpoints(conn, service, http_method, path, limit=10):
    """ENDPOINT nodes whose path template matches the (possibly concrete) path,
    most specific (fewest {vars}) first. Ambiguity across HTTP methods is
    returned, not resolved — the caller shows every handler."""
    sql = "SELECT * FROM nodes WHERE node_type = 'ENDPOINT'"
    params = []
    if service:
        sql += " AND service = ?"
        params.append(service)
    out = []
    for row in fetch_all(conn, sql, params):
        row = row_to_dict(row)
        meta = parse_metadata(row)
        tpl, hm = meta.get("path"), meta.get("http_method")
        if not tpl or (http_method and hm != http_method):
            continue
        if tpl.rstrip("/") == path.rstrip("/") or _template_regex(tpl).match(path):
            handler = find_exposing_method(conn, row["id"])
            cls = get_parent_class(conn, handler["id"]) if handler else None
            out.append({
                "endpoint_id": row["id"], "service": row["service"], "http_method": hm, "path": tpl,
                "controller": cls["name"] if cls else None,
                "handler": handler["name"] if handler else None,
                "handler_qualified_name": handler["qualified_name"] if handler else None,
                "file": handler["file_path"] if handler else None,
                "line": handler["start_line"] if handler else None,
                "_vars": len(_TEMPLATE_VAR_RE.findall(tpl)),
            })
    # Spring routes to the most specific mapping only: `/chat/message/unread-count` must not
    # also report `/chat/message/{messageId}` (which matches it as a wildcard) as an entry.
    fewest_vars = {}
    for e in out:
        key = (e["service"], e["http_method"])
        fewest_vars[key] = min(e["_vars"], fewest_vars.get(key, e["_vars"]))
    out = [e for e in out if e["_vars"] == fewest_vars[(e["service"], e["http_method"])]]
    out.sort(key=lambda e: (e["_vars"], e["service"] or "", e["http_method"] or ""))
    for e in out:
        e.pop("_vars")
    return out[:limit]


# --------------------------------------------------------------------------
# Per-method effects: DB / Kafka / Feign / WebClient / async
# --------------------------------------------------------------------------

def _split_spec(spec):
    spec = spec.strip()
    if "#" in spec:
        cls, meth = spec.split("#", 1)
    elif "." in spec:
        cls, meth = spec.rsplit(".", 1)
    else:
        return None, spec.split("(")[0]
    return cls, meth.split("(")[0]


def find_method_nodes(conn, spec, service=None, limit=10):
    """METHOD nodes for 'Class#method' | 'Class.method' | 'pkg.Class#method(sig)'.
    Overloads all match (a saga can't tell them apart from a name alone)."""
    cls, meth = _split_spec(spec)
    sql = "SELECT * FROM nodes WHERE node_type = 'METHOD' AND name = ?"
    params = [meth]
    if cls:
        sql += " AND (qualified_name LIKE ? OR qualified_name LIKE ?)"
        params += [f"{cls}#{meth}(%", f"%.{cls}#{meth}(%"]
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " LIMIT ?"
    params.append(limit)
    return [row_to_dict(r) for r in fetch_all(conn, sql, params)]


def _effect_rows(conn, method_id):
    return fetch_all(
        conn,
        "SELECT e.relation, e.target_name, e.metadata AS edge_meta, t.id AS tid, t.node_type AS ttype, "
        "t.name AS tname, t.service AS tservice, t.metadata AS tmeta "
        f"FROM edges e LEFT JOIN nodes t ON t.id = e.target_id WHERE e.source_id = ? "
        f"AND e.relation IN ({','.join('?' * len(EFFECT_RELATIONS))})",
        (method_id, *EFFECT_RELATIONS),
    )


def method_effects(conn, spec, service=None):
    matches = find_method_nodes(conn, spec, service)
    result = {"spec": spec, "matches": [], "db": [], "kafka_publishes": [], "kafka_consumes": [],
              "service_calls": [], "async": []}
    seen = set()
    for m in matches:
        result["matches"].append({"qualified_name": m["qualified_name"], "service": m["service"],
                                  "file": m["file_path"], "line": m["start_line"]})
        for r in _effect_rows(conn, m["id"]):
            r = row_to_dict(r)
            em = parse_metadata({"metadata": r["edge_meta"]})
            rel, conf = r["relation"], em.get("confidence")
            key = (rel, r["tname"] or r["target_name"], em.get("operation"), em.get("via"))
            if key in seen:
                continue
            seen.add(key)
            if rel in ("WRITES", "READS"):
                tmeta = parse_metadata({"metadata": r["tmeta"]})
                result["db"].append({
                    "access": "write" if rel == "WRITES" else "read", "operation": em.get("operation"),
                    "table": r["tname"], "engine": tmeta.get("engine") or "sql",
                    "via": em.get("via"), "entity": em.get("entity"), "confidence": conf,
                    **({"unresolved": r["target_name"]} if r["tname"] is None else {}),
                })
            elif rel == "PUBLISHES":
                result["kafka_publishes"].append({"topic": r["tname"], "via": em.get("via"), "confidence": conf})
            elif rel == "CONSUMES":
                result["kafka_consumes"].append({"topic": r["tname"], "via": em.get("via"), "confidence": conf})
            elif rel in _CALL_RELATIONS:
                call = {"kind": rel, "http_method": em.get("http_method"), "path": em.get("path"),
                        "target_prefix": em.get("target_prefix"), "confidence": conf}
                if r["tid"] is not None and r["ttype"] == "ENDPOINT":
                    handler = find_exposing_method(conn, r["tid"])
                    cls = get_parent_class(conn, handler["id"]) if handler else None
                    call.update({
                        "resolved": True, "service": r["tservice"],
                        "handler_qualified_name": handler["qualified_name"] if handler else None,
                        "controller": cls["name"] if cls else None, "handler": handler["name"] if handler else None,
                    })
                else:
                    call.update({"resolved": False,
                                 "reason": em.get("resolution") or "target service/endpoint not indexed",
                                 "target": r["target_name"]})
                result["service_calls"].append(call)
            elif rel in ("ASYNC_CALL", "RUNS"):
                result["async"].append({"kind": rel, "target": r["tname"] or r["target_name"]})
    return result


def method_dep_calls(conn, spec, service=None):
    """Typed calls a method makes on its injected dependencies (`dep.method()`),
    each resolved to the dependency type's class. These come from the field's
    DECLARED type (the same resolution Spring does), so unlike a name-matching
    call graph they can't bind to an unrelated same-named method."""
    calls, seen = [], set()
    for m in find_method_nodes(conn, spec, service):
        for e in fetch_all(conn, "SELECT metadata FROM edges WHERE source_id = ? AND relation = 'CALLS_DEP'", (m["id"],)):
            meta = parse_metadata({"metadata": e["metadata"]})
            dep_type, callee = meta.get("type"), meta.get("method")
            if not dep_type or (dep_type, callee) in seen:
                continue
            seen.add((dep_type, callee))
            cls = fetch_one(conn, "SELECT qualified_name FROM nodes WHERE node_type IN ('CLASS','INTERFACE') "
                                  "AND name = ? AND service = ? LIMIT 1", (dep_type, m["service"]))
            calls.append({"type": dep_type, "method": callee, "field": meta.get("field"),
                          "class_fqn": cls["qualified_name"] if cls else None})
    return {"spec": spec, "calls": calls}


# --------------------------------------------------------------------------
# Class facts: used to validate Code Cortex call edges against the KG's own
# wiring knowledge (Code Cortex over-approximates by name; a Spring bean can
# only be reached through an injected dependency).
# --------------------------------------------------------------------------

BEAN_ANNOTATIONS = {"Service", "Component", "Repository", "Controller", "RestController", "Configuration",
                    "ControllerAdvice", "RestControllerAdvice", "Aspect", "FeignClient"}
_BEAN_NAME_RE = re.compile(r"(Service|ServiceImpl|Impl|Repository|Dao|DaoService|Controller|Manager|Handler|Facade|Proxy|Client)$")


def class_facts(conn, classes, service=None):
    """{simple class name -> {is_bean, depends_on[], supertypes[], found}} for
    simple or fully-qualified names. Same simple name in one service is merged
    (rare, and merging only ever widens what's allowed)."""
    out = {}
    for spec in classes[:80]:
        simple = spec.rsplit(".", 1)[-1]
        if simple in out:
            continue
        sql = "SELECT * FROM nodes WHERE node_type IN ('CLASS','INTERFACE') AND name = ?"
        params = [simple]
        if service:
            sql += " AND service = ?"
            params.append(service)
        rows = [row_to_dict(r) for r in fetch_all(conn, sql + " LIMIT 6", params)]
        facts = {"found": bool(rows), "is_bean": False, "depends_on": [], "supertypes": []}
        deps, supers = set(), set()
        for r in rows:
            anns = set(parse_metadata(r).get("annotations") or [])
            facts["is_bean"] = facts["is_bean"] or bool(anns & BEAN_ANNOTATIONS) or r["node_type"] == "INTERFACE" \
                or bool(_BEAN_NAME_RE.search(simple))
            for e in fetch_all(
                    conn, "SELECT e.relation, e.target_name, t.name AS tname FROM edges e LEFT JOIN nodes t ON t.id = e.target_id "
                          "WHERE e.source_id = ? AND e.relation IN ('DEPENDS_ON','EXTENDS','IMPLEMENTS')", (r["id"],)):
                name = (e["tname"] or e["target_name"] or "").rsplit(".", 1)[-1]
                if name:
                    (deps if e["relation"] == "DEPENDS_ON" else supers).add(name)
        if not rows:
            facts["is_bean"] = bool(_BEAN_NAME_RE.search(simple))
        facts["depends_on"], facts["supertypes"] = sorted(deps), sorted(supers)
        out[simple] = facts
    return out


# --------------------------------------------------------------------------
# Kafka topic -> publishers / consumers
# --------------------------------------------------------------------------

def topic_flow(conn, topic, limit=25):
    topics = [row_to_dict(r) for r in fetch_all(
        conn, "SELECT * FROM nodes WHERE node_type = 'KAFKA_TOPIC' AND name = ?", (topic,))]
    if not topics:
        topics = [row_to_dict(r) for r in fetch_all(
            conn, "SELECT * FROM nodes WHERE node_type = 'KAFKA_TOPIC' AND name LIKE ? LIMIT 5", (f"%{topic}%",))]
    out = []
    for t in topics:
        entry = {"topic": t["name"], "publishers": [], "consumers": []}
        # Per relation: one shared LIMIT let a topic with many publishers
        # push every consumer out of the result.
        for relation, key in (("PUBLISHES", "publishers"), ("CONSUMES", "consumers")):
            rows = fetch_all(
                conn,
                "SELECT e.metadata AS edge_meta, s.qualified_name, s.service, s.file_path, s.start_line "
                "FROM edges e JOIN nodes s ON s.id = e.source_id WHERE e.target_id = ? AND e.relation = ? LIMIT ?",
                (t["id"], relation, limit))
            for r in rows:
                r = row_to_dict(r)
                em = parse_metadata({"metadata": r["edge_meta"]})
                entry[key].append({"method_qualified_name": r["qualified_name"], "service": r["service"],
                                   "file": r["file_path"], "line": r["start_line"], "via": em.get("via"),
                                   "confidence": em.get("confidence")})
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# UI trigger chain: endpoint <- API constant <- file <- component/service
#                   <- template event (click, ...) / route
# --------------------------------------------------------------------------

def _route_paths(conn, component_id, service):
    paths = []
    for r in fetch_all(conn, "SELECT s.id, s.name FROM edges e JOIN nodes s ON s.id = e.source_id "
                             "WHERE e.target_id = ? AND e.relation = 'ROUTES_TO' LIMIT 4", (component_id,)):
        segments, current = [r["name"]], r["id"]
        for _ in range(5):
            parent = fetch_one(conn, "SELECT s.id, s.name FROM edges e JOIN nodes s ON s.id = e.source_id "
                                     "WHERE e.target_id = ? AND e.relation = 'CONTAINS_ROUTE' LIMIT 1", (current,))
            if not parent:
                break
            segments.insert(0, parent["name"])
            current = parent["id"]
        paths.append("/".join(s.strip("/") for s in segments if s and s != "(empty)"))
    return paths


def _component_view(conn, comp):
    events = [row_to_dict(e) for e in fetch_all(
        conn, "SELECT name, file_path, start_line, metadata FROM nodes WHERE node_type = 'TEMPLATE_EVENT' "
              "AND service = ? AND json_extract(metadata, '$.component_name') = ? LIMIT 60", (comp["service"], comp["name"]))]
    return {
        "component": comp["name"], "ui_project": comp["service"], "file": comp["file_path"],
        "routes": _route_paths(conn, comp["id"], comp["service"]),
        "template_events": [{
            "event": (parse_metadata(e).get("event_name") or e["name"]),
            "handler": parse_metadata(e).get("handler_expression"),
            "method": parse_metadata(e).get("method_name"),
            "template": e["file_path"], "line": e["start_line"],
        } for e in events],
    }


def ui_trigger(conn, endpoint_id, limit=5):
    """Who in the UI reaches this endpoint. Component-level: the KG links a
    file to the constant it uses, not which method inside the file — so the
    events listed are that component's events, and unified-trace narrows them
    to the exact handler with Code Cortex's TS call graph."""
    by_name = {}
    for c in fetch_all(
            conn, "SELECT s.id, s.name, s.service, s.file_path FROM edges e JOIN nodes s ON s.id = e.source_id "
                  "WHERE e.target_id = ? AND e.relation = 'CALLS_API' LIMIT ?", (endpoint_id, limit * 3)):
        by_name.setdefault(c["name"], []).append(row_to_dict(c))
    chains = []
    for name, consts in list(by_name.items())[:limit]:
        chain = {"constant": name, "constant_file": consts[0]["file_path"],
                 "defined_in": sorted({c["service"] for c in consts}), "used_in": [], "components": []}
        files, seen_files = [], set()
        for c in consts:  # the same constant name can exist in several UI projects
            for f in fetch_all(
                    conn, "SELECT DISTINCT n.service, n.file_path FROM edges e JOIN nodes n ON n.id = e.source_id "
                          "WHERE e.target_id = ? AND e.relation = 'USES_API_CONSTANT' LIMIT 8", (c["id"],)):
                if (f["service"], f["file_path"]) not in seen_files:
                    seen_files.add((f["service"], f["file_path"]))
                    files.append(row_to_dict(f))
        components = {}
        for f in files[:8]:
            chain["used_in"].append({"ui_project": f["service"], "file": f["file_path"]})
            for n in fetch_all(conn, "SELECT * FROM nodes WHERE service = ? AND file_path = ? "
                                     "AND node_type IN ('NG_COMPONENT','NG_SERVICE') LIMIT 6", (f["service"], f["file_path"])):
                n = row_to_dict(n)
                if n["node_type"] == "NG_COMPONENT":
                    components[n["id"]] = n
                else:
                    for d in _service_dependents(conn, n["service"], n["name"]):
                        d["_via_service"] = n["name"]
                        components.setdefault(d["id"], d)
        for comp in list(components.values())[:10]:
            view = _component_view(conn, comp)
            if comp.get("_via_service"):
                view["via_service"] = comp["_via_service"]
            chain["components"].append(view)
        chains.append(chain)
    return chains


def _service_dependents(conn, ui_project, service_name, hops=2):
    """Components (through up to `hops` service-to-service injections) that
    depend on a UI service. Matched by NAME within the same UI project: the
    indexer resolves a dependency's target by name, and the same class name
    (e.g. MrdService) exists in several projects, so the resolved node id may
    belong to a different project's copy — following ids would miss real callers."""
    found, frontier = {}, [service_name]
    for _ in range(hops):
        next_frontier = []
        for name in frontier:
            # Drive from the project's own nodes (idx_nodes_service) and probe edges by source_id.
            # The old edges-first plan scanned every DEPENDS_ON edge (~130k): ~2s on a cold page
            # cache, repeated per hop, which intermittently blew the per-call query budget and
            # made the whole UI stage come back empty.
            for r in fetch_all(
                    conn, "SELECT DISTINCT s.* FROM nodes s CROSS JOIN edges e ON e.source_id = s.id "
                          "LEFT JOIN nodes t ON t.id = e.target_id "
                          "WHERE s.service = ? AND s.node_type IN ('NG_COMPONENT','NG_SERVICE') "
                          "AND e.relation = 'DEPENDS_ON' "
                          "AND (t.name = ? OR e.target_name = ?) LIMIT 12", (ui_project, name, name)):
                r = row_to_dict(r)
                if r["node_type"] == "NG_COMPONENT":
                    found.setdefault(r["id"], r)
                elif r["name"] not in frontier and r["name"] != service_name:
                    next_frontier.append(r["name"])
        frontier = next_frontier[:8]
        if not frontier:
            break
    return list(found.values())[:8]
