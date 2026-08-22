"""Confidence levels and the compact response-shaping helpers every tool
uses so results stay small and consistent (Section 17/18 of MCP.md).
"""

VERIFIED = "verified"    # backed directly by a real graph edge/annotation
INFERRED = "inferred"    # derived via a documented naming/structural heuristic
UNKNOWN = "unknown"      # relevant, but the graph has no signal either way


def fact(statement, confidence, evidence=None):
    """A single claim with its confidence and (optional) evidence pointer."""
    out = {"fact": statement, "confidence": confidence}
    if evidence:
        out["evidence"] = evidence
    return out


def evidence_from_node(node):
    """Build the compact evidence pointer used across tools from a nodes-table row dict."""
    if not node:
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
    return ev or None


def prune_empty(d):
    """Drop keys whose value is None, "", [], or {} (Rule 10: no
    unnecessary empty fields) — keeps 0 and False, those are real values.
    """
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def dedupe_by_key(items, key):
    """Stable de-duplication by a key function (Rule 11: never repeat the
    same service/class/table multiple times)."""
    seen = set()
    out = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out
