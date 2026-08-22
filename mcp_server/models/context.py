"""Shared response-shaping helpers: consistent compact dict shapes plus
the token-budgeting used by get_context_pack. Centralized here so every
tool builds the same shape rather than re-deriving field names.
"""
from .evidence import prune_empty

CHARS_PER_TOKEN_ESTIMATE = 4  # rough heuristic, documented in MCP.md — no tokenizer dependency


def estimate_tokens(text):
    """Approximate token count for budgeting get_context_pack — not exact,
    intentionally so: a real tokenizer is a dependency this project avoids
    for the same reason the indexer has none (see MCP.md "Token estimate")."""
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


def limit_list(items, limit):
    """Return (kept, total, truncated) — Rule 5: default result limits
    must be small, but callers should be able to tell truncation happened
    rather than silently think that's everything there is."""
    total = len(items)
    kept = items[:limit] if limit else items
    return kept, total, total > len(kept)


def with_truncation_note(payload, kept, total, field_name="results"):
    """Attach a note when a list was cut down, so the agent knows more
    exists rather than assuming `kept` is exhaustive."""
    if total > len(kept):
        payload["truncated"] = True
        payload["total_available"] = total
    return payload


def build_service_summary(name, service_type, summary):
    return prune_empty({"name": name, "type": service_type, "summary": summary})


def build_service_context(service, purpose, technology, apis_count, entities_count,
                           tables_count, dependencies, dependents, events):
    return prune_empty({
        "service": service,
        "purpose": purpose,
        "technology": technology or [],
        "apis_count": apis_count,
        "entities_count": entities_count,
        "tables_count": tables_count,
        "dependencies": dependencies or [],
        "dependents": dependents or [],
        "events": events or [],
    })


def build_dependency_edge(name, service, relation, direction, confidence, node_type=None):
    return prune_empty({
        "name": name,
        "service": service,
        "node_type": node_type,
        "relation": relation,
        "direction": direction,
        "confidence": confidence,
    })
