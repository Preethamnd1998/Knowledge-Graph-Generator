"""get_context_pack — a single, highly compressed Markdown package meant
to be an AI agent's *first* call for a topic, before reaching for
get_source_evidence on any one piece. Not in the spec's suggested file
tree verbatim (which folded it into no specific file); it gets its own
module here since it's substantial and composes flow + database queries.
"""
from .. import db
from ..errors import NotFoundError
from ..limits import require_non_empty
from ..logging_setup import tool_boundary
from ..models.context import estimate_tokens
from ..queries import flow_queries, search_queries

DEFAULT_MAX_TOKENS = 1500


def _render(topic, flow):
    lines = [f"# {topic.title()}", ""]

    lines += ["## Services"] + [f"- {s}" for s in flow["involved_services"]] + [""]

    if flow.get("entry_api") and flow["entry_api"].get("path"):
        lines += ["## API", f"{flow['entry_api'].get('http_method', '')} {flow['entry_api']['path']}".strip(), ""]

    main_classes = [f"{step['step']}" + ("()" if step["role"] == "method" else "")
                     for step in flow["chain"] if step["role"] in ("controller", "method")]
    if main_classes:
        lines += ["## Main Classes"] + [f"- {c}" for c in main_classes] + [""]

    if flow.get("database_tables"):
        lines += ["## Database"] + [f"- {t}" for t in flow["database_tables"]] + [""]

    if flow.get("chain"):
        lines += ["## Flow", "\n→ ".join(step["step"] for step in flow["chain"]), ""]

    # Pair up only the class-like steps (controller/service/repository) in
    # order — this mirrors the real DEPENDS_ON chain; method/event entries
    # are steps in the narrative but not themselves the source of an edge.
    class_like = [s["step"] for s in flow["chain"] if s["role"] in ("controller", "service", "repository")]
    relationships = [f"- {a} → {b}" for a, b in zip(class_like, class_like[1:])]
    if relationships:
        lines += ["## Important Relationships"] + relationships + [""]

    evidence = [step["step"] for step in flow["chain"] if step["role"] == "method"]
    if evidence:
        controller = next((s["step"] for s in flow["chain"] if s["role"] == "controller"), "")
        lines += ["## Evidence", f"- {controller}.{evidence[0]}()" if controller else f"- {evidence[0]}"]

    return "\n".join(lines).strip()


def _trim_to_budget(flow, max_tokens):
    """Progressively drop the least essential lists until the rendered
    pack fits max_tokens — chain/services are kept as long as possible
    since they're the core value; relationships/tables/events go first."""
    trimmed = dict(flow)
    trim_order = [
        ("events", []), ("database_tables", trimmed.get("database_tables", [])[:3]),
        ("involved_services", trimmed.get("involved_services", [])[:5]),
    ]
    for key, reduced in trim_order:
        text = _render(trimmed.get("flow", ""), trimmed)
        if estimate_tokens(text) <= max_tokens:
            break
        trimmed[key] = reduced
    return trimmed


@tool_boundary("get_context_pack")
def get_context_pack(topic: str, service: str = None, max_tokens: int = None) -> str:
    """Highly compressed Markdown context package for a topic (e.g.
    'appointment cancellation'): services, API, main classes, database
    tables, execution flow, key relationships, and an evidence pointer —
    optimized for LLM consumption in a single call. Trims itself to fit
    max_tokens (default 1500) rather than truncating mid-section."""
    topic = require_non_empty(topic, "topic")
    max_tokens = int(max_tokens) if max_tokens else DEFAULT_MAX_TOKENS

    with db.connect() as conn:
        if service:
            flow = flow_queries.get_business_flow(conn, service, topic)
        else:
            flow = None
            for candidate in search_queries.search_knowledge(conn, topic, limit=5):
                if candidate["type"] in ("business_flow", "api") and candidate.get("service"):
                    flow = flow_queries.get_business_flow(conn, candidate["service"], topic)
                    if flow:
                        break

    if flow is None:
        raise NotFoundError(
            f"Could not derive a business flow for '{topic}'"
            + (f" in service '{service}'" if service else "")
            + ". Try search_knowledge or find_api first."
        )

    flow["flow"] = topic
    pack = _render(topic, flow)
    if estimate_tokens(pack) > max_tokens:
        pack = _render(topic, _trim_to_budget(flow, max_tokens))
    return pack


def register(server):
    server.tool()(get_context_pack)
