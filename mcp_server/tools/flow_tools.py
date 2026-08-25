"""get_business_flow, trace_business_flow."""
from kg_indexer import flow_tracer

from .. import db
from ..errors import NotFoundError
from ..limits import clamp_depth, require_non_empty
from ..logging_setup import tool_boundary
from ..queries import flow_queries


@tool_boundary("get_business_flow")
def get_business_flow(service: str, flow: str) -> dict:
    """Compact end-to-end execution flow for a business operation (e.g.
    flow="cancellation"): UI callers of the entry API (if any), the
    controller -> service -> repository chain, involved services, classes,
    inferred database tables, naming-convention-detected events, and
    downstream calls (Feign/WebClient/RestTemplate to another backend
    service, Kafka publish/consume). Derived from real edges — there's no
    dedicated 'flow' node type, this is assembled on demand from the
    closest matching entry point."""
    service = require_non_empty(service, "service")
    flow = require_non_empty(flow, "flow")
    with db.connect() as conn:
        result = flow_queries.get_business_flow(conn, service, flow)
    if result is None:
        raise NotFoundError(
            f"No API or method in '{service}' matched flow '{flow}'. Try find_api or "
            f"find_service first to confirm names."
        )
    return result


@tool_boundary("trace_business_flow")
def trace_business_flow(target: str, direction: str = "both", depth: int = None) -> dict:
    """Trace the real, graph-backed business flow connected to `target` —
    a UI API constant (e.g. 'BillingApiConstant.BILLING_CONFIRM_PAYMENT'),
    an Angular/React component or service, a Java controller/class/method,
    a REST endpoint ('POST /opd/confirm/payment'), a Kafka topic, a DB
    table, or a Spring Batch Job/Step/Tasklet — by name. `direction`:
    'forward' (downstream effects), 'reverse' (upstream callers), or
    'both' (default, returned as two separate trees so a forward and a
    reverse hop are never mixed into one misleading path). `depth` caps
    hops per path (server-side maximum applies regardless of what's
    requested).

    Every hop carries an explicit, uppercase confidence — VERIFIED (an
    exact fact: a real @*Mapping/@FeignClient/@KafkaListener/exact
    resolved reference), INFERRED (resolved by simple-name matching or a
    documented structural convention — never presented as certain), or
    UNRESOLVED (the edge exists in source but its target could not be
    resolved to an indexed node — never guessed). A VERIFIED hop's
    `evidence` points at the real file/line/symbol; an INFERRED or
    UNRESOLVED hop's `reason` explains why. `confidence_summary` gives a
    quick VERIFIED/INFERRED/UNRESOLVED hop count across the whole result —
    check it before treating a flow as fully verified: any non-zero
    INFERRED/UNRESOLVED count means part of the path is not a certain fact.

    Distinct from get_business_flow (service + free-text flow keyword —
    one entry point's controller->service->repository chain only): this
    starts from a specifically named node of any kind and walks every real
    edge the graph has from there, in either direction, to whatever depth
    is requested — including UI callers, Feign/WebClient/RestTemplate
    calls to other services, Kafka publish/consume, and Spring Batch
    Job->Step->Tasklet chains, none of which get_business_flow covers on
    its own."""
    target = require_non_empty(target, "target")
    direction = direction if direction in ("forward", "reverse", "both") else "both"
    config = db.get_config()
    depth = clamp_depth(depth, config, default=flow_tracer.DEFAULT_MAX_DEPTH)
    with db.connect() as conn:
        result = flow_queries.trace_business_flow(conn, target, direction=direction, max_depth=depth)
    if not result.get("resolved"):
        raise NotFoundError(result.get(
            "message", f"No node matching '{target}' found in the knowledge graph."))
    return result


def register(server):
    server.tool()(get_business_flow)
    server.tool()(trace_business_flow)
