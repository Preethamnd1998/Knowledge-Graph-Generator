"""Business flow tracer — a read-only traversal layer on top of the
existing nodes/edges graph (kg_indexer/db.py). Does not add any new node
or edge types and does not change how any parser resolves anything; it
only *walks* the edges the parsers/resolvers already created, in either
narrative direction, and reports exactly what it found with an explicit
confidence per hop. See AGENTS.md "Business flow tracing" for the design
rationale this module implements.

Shared by example_queries.py (CLI) and mcp_server/queries/flow_queries.py
(MCP tool) so there is exactly one traversal implementation — both callers
pass in a sqlite3 connection with row_factory=sqlite3.Row (true of both
example_queries.connect() and mcp_server.db.connect()).

Confidence taxonomy (exactly these three, uppercase, per spec):

    VERIFIED   — an immediate structural fact recorded at parse time
                 (CONTAINS, EXPOSES, file->class/API_CONSTANT DECLARES),
                 or an *exact* match resolution (CALLS_API, FEIGN_CALL /
                 WEBCLIENT_CALL / RESTTEMPLATE_CALL, Java IMPORTS, and
                 anything spring_flow_parser.py itself already marked
                 metadata.confidence="verified", e.g. @KafkaListener,
                 Job->Step->Tasklet bean wiring).
    INFERRED   — resolved by *simple-name* matching against a per-language
                 registry (EXTENDS/IMPLEMENTS/DEPENDS_ON/USES_API_CONSTANT/
                 USES_HOOK/RENDERS/EXPORTS, and an NgModule's
                 declarations/imports/exports array entries), or anything
                 spring_flow_parser.py marked "inferred" (builder-pattern
                 Job/Step wiring, JobLauncher.run() targets, Kafka
                 topic/table extraction from a call-site pattern).
    UNRESOLVED — the edge exists (something in source really does
                 reference a name) but the target never resolved to an
                 indexed node — target_id is NULL. Never upgraded to a
                 guess; target_name is kept so the gap stays visible.

Never invents a relationship: only edges that already exist in the graph
are ever walked. When nothing is known past a node, the path simply ends
there rather than being padded with a placeholder.
"""
import json

VERIFIED = "VERIFIED"
INFERRED = "INFERRED"
UNRESOLVED = "UNRESOLVED"

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}

# Relations resolved by GraphDB.resolve_pending_edges (simple/short-name
# matching against a NameRegistry) — see AGENTS.md "Resolution passes".
# Once resolved these are INFERRED, never VERIFIED, because a short-name
# match is not guaranteed to be the *only* possible target (the registry
# only records "which candidate was picked", not certainty).
_SIMPLE_NAME_RESOLVED_RELATIONS = {
    "EXTENDS", "IMPLEMENTS", "DEPENDS_ON", "USES_API_CONSTANT",
    "USES_HOOK", "RENDERS", "EXPORTS",
}

# (relation, edge_end, applicable_modes) — the only transitions the tracer
# will ever walk. `edge_end`: "out" = the current node is edges.source_id
# (look at its outgoing edges of this relation); "in" = the current node is
# edges.target_id (look at its incoming edges). Modes: which narrative
# direction(s) this transition makes sense in. A transition simply returns
# zero rows when it doesn't apply to a given node's real node_type — no
# per-node_type gating needed here, the schema itself decides.
TRANSITIONS = (
    # UI API constant <-> backend endpoint
    ("CALLS_API", "out", ("forward",)),
    ("CALLS_API", "in", ("reverse",)),
    # UI file <-> API constant it references
    ("USES_API_CONSTANT", "out", ("forward",)),
    ("USES_API_CONSTANT", "in", ("reverse",)),
    # file <-> class/component/API_CONSTANT it declares (bridge, both ways
    # needed in both narrative directions — see module docstring / AGENTS.md)
    ("DECLARES", "out", ("forward", "reverse")),
    ("DECLARES", "in", ("forward", "reverse")),
    # endpoint <-> the controller method that exposes it
    ("EXPOSES", "out", ("forward", "reverse")),
    ("EXPOSES", "in", ("forward", "reverse")),
    # method <-> containing class (bridge)
    ("CONTAINS", "out", ("forward", "reverse")),
    ("CONTAINS", "in", ("forward", "reverse")),
    # service/DI dependency chain
    ("DEPENDS_ON", "out", ("forward",)),
    ("DEPENDS_ON", "in", ("reverse",)),
    # database
    ("WRITES", "out", ("forward",)),
    ("WRITES", "in", ("reverse",)),
    ("READS", "out", ("forward",)),
    ("READS", "in", ("reverse",)),
    # kafka
    ("PUBLISHES", "out", ("forward",)),
    ("PUBLISHES", "in", ("reverse",)),
    ("CONSUMES", "out", ("forward", "reverse")),
    ("CONSUMES", "in", ("forward", "reverse")),
    # backend -> backend
    ("FEIGN_CALL", "out", ("forward",)),
    ("FEIGN_CALL", "in", ("reverse",)),
    ("WEBCLIENT_CALL", "out", ("forward",)),
    ("WEBCLIENT_CALL", "in", ("reverse",)),
    ("RESTTEMPLATE_CALL", "out", ("forward",)),
    ("RESTTEMPLATE_CALL", "in", ("reverse",)),
    # async
    ("ASYNC_CALL", "out", ("forward",)),
    ("ASYNC_CALL", "in", ("reverse",)),
    # spring batch
    ("DEFINES_BEAN", "out", ("forward",)),
    ("DEFINES_BEAN", "in", ("reverse",)),
    ("RUNS", "out", ("forward",)),
    ("RUNS", "in", ("reverse",)),
    ("HAS_STEP", "out", ("forward",)),
    ("HAS_STEP", "in", ("reverse",)),
    ("USES_TASKLET", "out", ("forward",)),
    ("USES_TASKLET", "in", ("reverse",)),
)

# When expanding CLASS --CONTAINS--> METHOD, only follow methods that
# themselves carry at least one of these outgoing relations — a class can
# have dozens of methods with no business-flow significance at all
# (getters, private helpers, ...); without this filter that fan-out would
# dominate every trace through a service/controller/repository class with
# no way to bound it sensibly. Never applied to any other transition.
_METHOD_FLOW_RELATIONS = (
    "EXPOSES", "FEIGN_CALL", "WEBCLIENT_CALL", "RESTTEMPLATE_CALL",
    "PUBLISHES", "CONSUMES", "WRITES", "READS", "RUNS", "ASYNC_CALL",
)

MAX_CHILDREN_PER_NODE = 12
DEFAULT_MAX_DEPTH = 3
MAX_ALLOWED_DEPTH = 6
DEFAULT_MAX_PATHS = 25
DEFAULT_MAX_NODES = 400
DEFAULT_ROOT_LIMIT = 5


def _meta(row_or_dict):
    raw = row_or_dict.get("metadata") if isinstance(row_or_dict, dict) else row_or_dict["metadata"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _get_node(conn, node_id):
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def _placeholder(name):
    return {"id": None, "node_type": None, "name": name, "qualified_name": None, "service": None}


def classify_confidence(relation, source_node_type, target_id, edge_metadata):
    """Confidence for one edge, given the relation, the node_type of the
    edge's real source (edges.source_id — needed because DECLARES means two
    different things depending on who's on the source side, see below),
    whether target_id resolved, and the edge's own metadata (spring_flow_parser
    and the API-mapping resolvers already stamp verified/inferred/unknown/
    resolved on their own edges — that's authoritative when present)."""
    conf = (edge_metadata or {}).get("confidence")
    if conf == "verified":
        return VERIFIED
    if conf == "inferred":
        return INFERRED
    if conf == "unknown":
        return UNRESOLVED
    if conf == "resolved":
        return VERIFIED

    if relation == "DECLARES":
        # file -> class/component/API_CONSTANT is an immediate structural
        # fact; NgModule -> declarations[]/imports[]/exports[] entry is a
        # simple-name-resolved array element (target_name at creation).
        if source_node_type == "FILE":
            return VERIFIED if target_id is not None else UNRESOLVED
        return INFERRED if target_id is not None else UNRESOLVED

    if relation in _SIMPLE_NAME_RESOLVED_RELATIONS:
        return INFERRED if target_id is not None else UNRESOLVED

    # CONTAINS, EXPOSES, CALLS_API, IMPORTS (java, exact-path resolved),
    # FEIGN_CALL/WEBCLIENT_CALL/RESTTEMPLATE_CALL (exact qualified_name
    # resolved) — either an immediate structural fact or an exact match.
    return VERIFIED if target_id is not None else UNRESOLVED


def _reason_for(relation, confidence, edge_metadata):
    meta = edge_metadata or {}
    if meta.get("reason"):
        return meta["reason"]
    if meta.get("resolution") and meta["resolution"] != "resolved":
        return f"resolution={meta['resolution']}"
    if confidence == VERIFIED:
        return None
    if confidence == INFERRED:
        if relation in _SIMPLE_NAME_RESOLVED_RELATIONS or relation == "DECLARES":
            return ("resolved by simple-name matching against a same-language name "
                    "registry, not an exact source reference — see AGENTS.md "
                    "'Resolution passes'.")
        return "derived from a documented naming/structural convention, not an explicit source fact."
    if confidence == UNRESOLVED:
        return ("referenced in source but the target could not be resolved to an "
                "indexed node — likely an external library/service, or a symbolic "
                "value the indexer could not statically resolve. Source verification required.")
    return None


def _evidence_for(node):
    if not node or node.get("id") is None:
        return None
    ev = {}
    if node.get("file_path"):
        ev["file"] = node["file_path"]
    if node.get("name"):
        ev["symbol"] = node["name"]
    if node.get("start_line") is not None:
        ev["line_start"] = node["start_line"]
    if node.get("end_line") is not None:
        ev["line_end"] = node["end_line"]
    if node.get("service"):
        ev["service"] = node["service"]
    return ev or None


def _node_ref(node):
    if not node or node.get("id") is None:
        return {"resolved": False, "name": (node or {}).get("name")}
    return {
        "resolved": True,
        "id": node["id"],
        "node_type": node.get("node_type"),
        "name": node.get("name"),
        "qualified_name": node.get("qualified_name"),
        "service": node.get("service"),
    }


_CLASS_FAMILY = ("CLASS", "INTERFACE", "ENUM")


def _keep_transition(conn, relation, neighbor, end):
    """CONTAINS and DECLARES each mean more than one structural thing in
    this schema (see AGENTS.md) — only the business-flow-relevant sense of
    each is walked here, everything else is dropped before it ever reaches
    the fan-out cap:

    - CONTAINS also covers SERVICE->FILE and NG_SUBPROJECT->FILE (the
      whole-service file listing) — only the CLASS<->METHOD sense is a
      business-flow bridge; the service/subproject sense would fan out to
      every file in a service/subproject (thousands of nodes) the moment a
      trace reaches any FILE, CLASS, or SERVICE-ish node, which is exactly
      the "exponential traversal" this tracer must prevent.
    - DECLARES also covers FILE->API_CONSTANT (there can be 100+ constant
      fields declared in one constants file) — walking that forward from a
      FILE would pull in every unrelated constant that merely happens to
      share a source file with the one actually being traced. The reverse
      direction (a specific API_CONSTANT -> the one file that declares it)
      is unaffected and stays a real, useful, non-explosive bridge.
    """
    neighbor_type = (neighbor or {}).get("node_type")
    if relation == "CONTAINS":
        return neighbor_type == "METHOD" if end == "out" else neighbor_type in _CLASS_FAMILY
    if relation == "DECLARES" and end == "out":
        return neighbor_type != "API_CONSTANT"
    return True


def _expand(conn, node, mode):
    """All (relation, edge_dict, neighbor_node_dict, end) transitions valid
    from `node` in narrative `mode` ('forward'/'reverse'), already deduped
    and capped to MAX_CHILDREN_PER_NODE (resolved neighbors prioritized
    over unresolved ones when trimming)."""
    out = []
    for relation, end, modes in TRANSITIONS:
        if mode not in modes:
            continue
        if end == "out":
            rows = conn.execute(
                "SELECT * FROM edges WHERE source_id = ? AND relation = ?",
                (node["id"], relation),
            ).fetchall()
            for row in rows:
                edge = dict(row)
                neighbor = _get_node(conn, edge["target_id"]) if edge["target_id"] is not None \
                    else _placeholder(edge["target_name"])
                if _keep_transition(conn, relation, neighbor, "out"):
                    out.append((relation, edge, neighbor, "out"))
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE target_id = ? AND relation = ?",
                (node["id"], relation),
            ).fetchall()
            for row in rows:
                edge = dict(row)
                neighbor = _get_node(conn, edge["source_id"])
                if _keep_transition(conn, relation, neighbor, "in"):
                    out.append((relation, edge, neighbor, "in"))

    if node.get("node_type") in _CLASS_FAMILY:
        out = [
            item for item in out
            if not (item[0] == "CONTAINS" and item[3] == "out")
            or _method_has_flow_edges(conn, item[2]["id"])
        ]

    out.sort(key=lambda item: (item[2].get("id") is None, item[2].get("id") or 0))
    return out[:MAX_CHILDREN_PER_NODE]


def _method_has_flow_edges(conn, method_id):
    placeholders = ",".join("?" for _ in _METHOD_FLOW_RELATIONS)
    row = conn.execute(
        f"SELECT 1 FROM edges WHERE source_id = ? AND relation IN ({placeholders}) LIMIT 1",
        (method_id, *_METHOD_FLOW_RELATIONS),
    ).fetchone()
    return row is not None


def _build_hop(current, neighbor, relation, end, mode, depth, edge):
    meta = _meta(edge)
    # classify_confidence needs the node_type of the edge's REAL source
    # (edges.source_id) and whether target_id resolved — both independent
    # of which way we're walking the edge (end == "in" means `current` is
    # actually the edge's target, `neighbor` is its source).
    source_node_type = current.get("node_type") if end == "out" else neighbor.get("node_type")
    confidence = classify_confidence(relation, source_node_type, edge["target_id"], meta)
    evidence_node = current if end == "out" else neighbor
    return {
        "hop_index": depth,
        "path_type": "DIRECT" if depth == 1 else "TRANSITIVE",
        "source": _node_ref(current),
        "target": _node_ref(neighbor),
        "relation": relation,
        "edge": relation,  # alias — this codebase's schema calls the edges.relation column "relation" everywhere else (see AGENTS.md); kept as "edge" too since that's the literal field name requested for the flow-tracer JSON shape
        "direction": mode,
        "edge_direction": end,
        "confidence": confidence,
        "evidence": _evidence_for(evidence_node),
        "reason": _reason_for(relation, confidence, meta),
    }


def _dfs(conn, node, mode, depth, max_depth, path_so_far, path_ids_seen, budget, paths_out, max_paths):
    if budget["nodes"] >= budget["max_nodes"] or len(paths_out) >= max_paths:
        if path_so_far:
            paths_out.append(list(path_so_far))
        return

    children = _expand(conn, node, mode)
    if not children:
        if path_so_far:
            paths_out.append(list(path_so_far))
        return

    for relation, edge, neighbor, end in children:
        if len(paths_out) >= max_paths:
            return
        budget["nodes"] += 1
        hop = _build_hop(node, neighbor, relation, end, mode, depth, edge)
        neighbor_id = neighbor.get("id")

        if neighbor_id is not None and neighbor_id in path_ids_seen:
            # Cycle: record that this hop closes a loop, but do not recurse.
            hop = dict(hop, cycle=True)
            paths_out.append(path_so_far + [hop])
            continue

        new_path = path_so_far + [hop]
        if neighbor_id is None or depth >= max_depth:
            paths_out.append(new_path)
            continue

        path_ids_seen.add(neighbor_id)
        _dfs(conn, neighbor, mode, depth + 1, max_depth, new_path, path_ids_seen, budget, paths_out, max_paths)
        path_ids_seen.discard(neighbor_id)


def resolve_target(conn, target, limit=10):
    """Fuzzy-resolve a CLI/MCP target string to candidate start nodes,
    trying progressively looser matches — an EXACT match of any kind
    (qualified_name, endpoint "METHOD /path", or plain node name, which
    also covers a full "ClassName.FIELD" API constant name) always wins
    over a fuzzy/partial one, regardless of node type: a bare field name
    like "ADMISSION_STATUS_UPDATE" must resolve to an exact KAFKA_TOPIC of
    that name before it's even considered as a partial match against some
    unrelated API_CONSTANT's field. Returns a list of node dicts (possibly
    more than one — an ambiguous target traces from every candidate rather
    than silently guessing one)."""
    target = (target or "").strip()
    if not target:
        return []

    rows = conn.execute("SELECT * FROM nodes WHERE qualified_name = ?", (target,)).fetchall()
    if rows:
        return [dict(r) for r in rows][:limit]

    parts = target.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in HTTP_METHODS:
        method, path = parts[0].upper(), parts[1].strip()
        if not path.startswith("/"):
            path = "/" + path
        rows = conn.execute(
            "SELECT * FROM nodes WHERE node_type = 'ENDPOINT' AND "
            "json_extract(metadata, '$.http_method') = ? AND json_extract(metadata, '$.path') = ?",
            (method, path),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows][:limit]

    rows = conn.execute(
        "SELECT * FROM nodes WHERE name = ? AND node_type != 'FILE' ORDER BY node_type",
        (target,),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows][:limit]

    rows = conn.execute(
        "SELECT * FROM nodes WHERE node_type = 'API_CONSTANT' AND "
        "json_extract(metadata, '$.normalized_path') = ?",
        (target.strip("/"),),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows][:limit]

    rows = conn.execute(
        "SELECT * FROM nodes WHERE node_type = 'API_CONSTANT' AND name LIKE ?",
        (f"%.{target}",),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows][:limit]

    if "/" in target:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE node_type = 'ENDPOINT' AND json_extract(metadata, '$.path') LIKE ?",
            (f"%{target.strip('/')}%",),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows][:limit]

    rows = conn.execute(
        "SELECT * FROM nodes WHERE name LIKE ? AND node_type != 'FILE' ORDER BY node_type LIMIT ?",
        (f"%{target}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _confidence_summary(result_roots):
    counts = {VERIFIED: 0, INFERRED: 0, UNRESOLVED: 0}
    for root in result_roots:
        for paths in (root["forward_paths"], root["reverse_paths"]):
            for path in paths:
                for hop in path:
                    counts[hop["confidence"]] = counts.get(hop["confidence"], 0) + 1
    return counts


def trace(conn, target, direction="both", max_depth=DEFAULT_MAX_DEPTH,
          max_nodes=DEFAULT_MAX_NODES, max_paths=DEFAULT_MAX_PATHS, root_limit=DEFAULT_ROOT_LIMIT):
    """Trace the business flow connected to `target`.

    direction: "forward" (downstream effects), "reverse" (upstream
    callers), or "both" (both, as two separate trees anchored at the same
    start node(s) — never mixed step-by-step within one path, which would
    produce a nonsensical zigzag).
    max_depth: hops per path (clamped to MAX_ALLOWED_DEPTH).
    Cycle/duplicate protection: a node already on the current path is never
    revisited within that path (prevents infinite loops); total expansion
    work is capped by max_nodes and the number of emitted paths by
    max_paths, so traversal is always bounded regardless of fan-out.
    """
    if direction not in ("forward", "reverse", "both"):
        direction = "both"
    max_depth = max(1, min(int(max_depth or DEFAULT_MAX_DEPTH), MAX_ALLOWED_DEPTH))

    candidates = resolve_target(conn, target, limit=root_limit + 5)
    if not candidates:
        return {
            "target": target, "resolved": False, "results": [],
            "message": f"No node matching '{target}' found in the knowledge graph.",
        }

    candidates_truncated = len(candidates) > root_limit
    candidates = candidates[:root_limit]

    result_roots = []
    for cand in candidates:
        budget = {"nodes": 0, "max_nodes": max_nodes}
        forward_paths, reverse_paths = [], []
        if direction in ("forward", "both"):
            _dfs(conn, cand, "forward", 1, max_depth, [], {cand["id"]}, budget, forward_paths, max_paths)
        if direction in ("reverse", "both"):
            _dfs(conn, cand, "reverse", 1, max_depth, [], {cand["id"]}, budget, reverse_paths, max_paths)
        result_roots.append({
            "start_node": _node_ref(cand),
            "forward_paths": forward_paths,
            "reverse_paths": reverse_paths,
        })

    return {
        "target": target,
        "resolved": True,
        "ambiguous": len(candidates) > 1,
        "candidates_truncated": candidates_truncated,
        "direction": direction,
        "max_depth": max_depth,
        "results": result_roots,
        "confidence_summary": _confidence_summary(result_roots),
    }
