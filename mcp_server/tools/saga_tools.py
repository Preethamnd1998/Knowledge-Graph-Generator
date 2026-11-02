"""saga_entry, saga_method_effects, saga_topic_flow, saga_ui_trigger.

Building blocks for unified-trace's `saga` tool (UI action -> backend ->
cross-service hops -> DB). Each returns graph facts with an explicit
confidence/resolution; none walks method-to-method calls (that's Code Cortex).
"""
from .. import db
from ..errors import InvalidInputError, NotFoundError
from ..limits import clamp_limit
from ..logging_setup import tool_boundary
from ..queries import saga_queries

MAX_METHODS_PER_CALL = 40


@tool_boundary("saga_entry")
def saga_entry(target: str, method: str = None, service: str = None) -> dict:
    """Resolve an API to its backend handler(s). `target` may be a full
    gateway URL ('https://host/api-gateway/ipd-clinical/settings/6a4b...'),
    'PUT /settings/{id}', a bare path, or 'gateway-prefix/path'; a concrete id
    segment matches its {var} template. Handlers on the same path with
    different HTTP methods are ALL returned (never silently picked)."""
    if not target or not target.strip():
        raise InvalidInputError("'target' is required.")
    with db.connect() as conn:
        svc, http_method, path = saga_queries.normalize_target(conn, target, method, service)
        candidates = saga_queries.find_entry_endpoints(conn, svc, http_method, path)
    if not candidates:
        raise NotFoundError(
            f"No endpoint matches path '{path}'"
            + (f" (service {svc})" if svc else "") + (f" ({http_method})" if http_method else "")
            + " in the knowledge graph — the owning service may not be indexed.")
    return {"normalized": {"service": svc, "http_method": http_method, "path": path},
            "ambiguous": len(candidates) > 1, "candidates": candidates}


@tool_boundary("saga_method_effects")
def saga_method_effects(methods: list[str], service: str = None) -> dict:
    """For each 'Class#method' (or 'Class.method' / 'pkg.Class#method(sig)'):
    the DB reads/writes (SQL and Mongo, with operation + confidence), Kafka
    publishes/consumes, Feign/WebClient/RestTemplate calls (resolved to the
    target service's handler, or unresolved WITH the reason), and async
    operations that method performs. Batch several methods per call."""
    if not methods:
        raise InvalidInputError("'methods' must be a non-empty list.")
    if len(methods) > MAX_METHODS_PER_CALL:
        raise InvalidInputError(f"At most {MAX_METHODS_PER_CALL} methods per call.")
    with db.connect() as conn:
        return {"results": [saga_queries.method_effects(conn, m, service) for m in methods]}


@tool_boundary("saga_topic_flow")
def saga_topic_flow(topic: str) -> dict:
    """Every publisher and consumer (service, method, file:line, confidence)
    of a Kafka topic — the cross-service hop for an async saga. Exact name
    first, then a substring match. A topic with only one side indexed is
    reported as-is (the other side's service may not be checked out)."""
    if not topic or not topic.strip():
        raise InvalidInputError("'topic' is required.")
    with db.connect() as conn:
        flows = saga_queries.topic_flow(conn, topic.strip())
    if not flows:
        raise NotFoundError(f"No Kafka topic matching '{topic}' in the knowledge graph.")
    return {"topics": flows}


@tool_boundary("saga_ui_trigger")
def saga_ui_trigger(endpoint_id: int, limit: int = None) -> dict:
    """UI side of a saga for an endpoint_id from saga_entry: the UI API
    constant(s) that call it, the file(s) using them, and the component(s)
    (directly, or via an injected UI service) with their routes and template
    events. Component-level — the graph doesn't record which method in a file
    uses the constant. An empty `chains` list means no indexed UI project
    calls this endpoint (the UI library may not be checked out)."""
    config = db.get_config()
    limit = clamp_limit(limit, config)
    with db.connect() as conn:
        return {"chains": saga_queries.ui_trigger(conn, endpoint_id, limit=min(limit, 8))}


@tool_boundary("saga_method_calls")
def saga_method_calls(methods: list[str], service: str = None) -> dict:
    """For each 'Class#method' (or 'pkg.Class#method(sig)'): the calls it makes on
    its INJECTED dependencies (@Autowired/@Inject and constructor-injected
    fields), each resolved to the dependency type's class (`class_fqn`, or null if
    the type isn't in the graph). Typed by the field's declared type — exact for
    Spring wiring, and it works where a name-matching call graph misses or
    mis-binds the call. Does not cover static calls or calls on local objects."""
    if not methods:
        raise InvalidInputError("'methods' must be a non-empty list.")
    if len(methods) > MAX_METHODS_PER_CALL:
        raise InvalidInputError(f"At most {MAX_METHODS_PER_CALL} methods per call.")
    with db.connect() as conn:
        return {"results": [saga_queries.method_dep_calls(conn, m, service) for m in methods]}


@tool_boundary("saga_class_facts")
def saga_class_facts(classes: list[str], service: str = None) -> dict:
    """Wiring facts for classes (simple or fully-qualified names): whether each
    is a Spring bean, the classes it depends on (@Autowired/@Inject AND
    constructor-injected `private final` fields) and its supertypes. Used to
    reject a call edge from another analyzer that the KG's dependency graph
    rules out. `found: false` means the class isn't in the graph (facts unknown)."""
    if not classes:
        raise InvalidInputError("'classes' must be a non-empty list.")
    with db.connect() as conn:
        return {"classes": saga_queries.class_facts(conn, classes, service)}


def register(server):
    server.tool()(saga_method_calls)
    server.tool()(saga_class_facts)
    server.tool()(saga_entry)
    server.tool()(saga_method_effects)
    server.tool()(saga_topic_flow)
    server.tool()(saga_ui_trigger)
