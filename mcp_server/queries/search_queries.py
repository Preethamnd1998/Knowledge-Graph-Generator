"""Query logic backing search_knowledge — the general-purpose entry point.

No LLM/embeddings live in the MCP server (see MCP.md "Design
principles"); "understanding" the query is a deterministic keyword-
overlap heuristic across a few knowledge types, ranked so business-flow
and relationship information outranks an isolated class/component match
(spec requirement: prefer flows over isolated source nodes).
"""
from .api_queries import find_apis
from .flow_queries import get_business_flow
from .common import find_nodes_fuzzy, parse_metadata
from .service_queries import search_services

_STOPWORDS = {
    "a", "an", "the", "what", "happens", "when", "is", "are", "to", "of",
    "in", "on", "does", "do", "how", "for", "and", "or", "with", "this",
    "that", "it", "if",
}
_TYPE_WEIGHT = {"business_flow": 1.0, "api": 0.85, "service": 0.7, "node": 0.55}


def _tokens(query):
    return [t for t in "".join(c if c.isalnum() else " " for c in query.lower()).split()
            if t and t not in _STOPWORDS]


def search_knowledge(conn, query, limit=10):
    tokens = _tokens(query)
    if not tokens:
        return []

    candidates = []
    seen = set()

    def add(kind, name, summary, base_score, extra=None):
        key = (kind, name)
        if key in seen:
            return
        seen.add(key)
        item = {"type": kind, "name": name, "summary": summary,
                "relevance": round(base_score * _TYPE_WEIGHT.get(kind, 0.5), 2)}
        if extra:
            item.update(extra)
        candidates.append(item)

    keyword = " ".join(tokens)

    api_matches = find_apis(conn, query=keyword, limit=5)
    if api_matches:
        top = api_matches[0]
        meta = parse_metadata(top)
        flow = get_business_flow(conn, top["service"], keyword) if top.get("service") else None
        if flow:
            add("business_flow", f"{top['service']}:{keyword}",
                f"{meta.get('http_method')} {meta.get('path')} -> " + " -> ".join(
                    step["step"] for step in flow["chain"][:5]
                ),
                1.0, {"service": top["service"]})
        for api in api_matches[:3]:
            m = parse_metadata(api)
            add("api", api["name"], f"{api.get('service')} - {m.get('http_method')} {m.get('path')}", 0.8,
                {"service": api.get("service")})

    for svc in search_services(conn, keyword, limit=3):
        if any(t in svc["name"].lower() for t in tokens):
            add("service", svc["name"], svc["summary"], 0.9, {"project_type": svc.get("project_type")})

    if len(candidates) < limit:
        for node in find_nodes_fuzzy(conn, keyword, limit=limit):
            add("node", node["name"],
                f"{node.get('node_type')} in {node.get('service') or 'unknown service'}", 0.5,
                {"node_type": node.get("node_type"), "service": node.get("service")})

    candidates.sort(key=lambda c: -c["relevance"])
    return candidates[:limit]
