"""Query logic backing find_api / get_api_context.

Note on request/response DTOs: the indexer stores each METHOD's parameter
*types* (with generics like ``List<Foo>`` collapsed to ``List``) but not
which Spring annotation (``@RequestBody`` vs ``@PathVariable`` vs
``@RequestHeader``) applied to which one — that mapping is discarded
during parsing, not just hard to query. So "request DTO" here is a
best-effort guess (first non-primitive/non-framework parameter type),
always marked ``inferred``, never presented as verified.
"""
import re

from ..db import fetch_all, row_to_dict
from ..models.evidence import prune_empty
from .common import (
    class_dependency_chain, find_exposing_method, get_parent_class,
    inferred_table_name, parse_metadata,
)

_PRIMITIVE_OR_FRAMEWORK_TYPES = {
    "String", "int", "long", "boolean", "double", "float", "Integer", "Long",
    "Boolean", "Double", "Float", "Void", "void", "Object",
    "HttpHeaders", "HttpServletRequest", "HttpServletResponse", "Authentication",
    "Principal", "Pageable", "MultipartFile", "ResponseEntity", "List", "Map",
    "Set", "Optional", "UUID",
}
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_]\w*$')


def _clean_param_types(raw_params):
    """Drop comment-artifact entries the regex parser occasionally
    captures from multi-line signatures with a commented-out old
    parameter (see README "Known limitations") — anything that isn't a
    clean identifier is discarded rather than shown as-is."""
    return [p for p in (raw_params or []) if p and _IDENTIFIER_RE.match(p)]


def _guess_dto(candidates):
    for c in candidates:
        if c not in _PRIMITIVE_OR_FRAMEWORK_TYPES:
            return c
    return None


def find_apis(conn, query=None, path=None, method=None, service=None, limit=10):
    sql = (
        "SELECT e.id, e.name, e.service, e.file_path, e.metadata, "
        "m.name AS method_name, c.name AS controller_name FROM nodes e "
        "LEFT JOIN edges ex ON ex.target_id = e.id AND ex.relation = 'EXPOSES' "
        "LEFT JOIN nodes m ON m.id = ex.source_id "
        "LEFT JOIN edges co ON co.target_id = m.id AND co.relation = 'CONTAINS' "
        "LEFT JOIN nodes c ON c.id = co.source_id "
        "WHERE e.node_type = 'ENDPOINT'"
    )
    params = []
    if service:
        sql += " AND e.service = ?"
        params.append(service)
    if method:
        sql += " AND json_extract(e.metadata, '$.http_method') = ?"
        params.append(method.upper())
    if path:
        sql += " AND json_extract(e.metadata, '$.path') LIKE ?"
        params.append(f"%{path}%")

    rows = [row_to_dict(r) for r in fetch_all(conn, sql, params)]

    if query:
        tokens = [t.lower() for t in query.split() if t]

        def score(row):
            haystack = " ".join(filter(None, [row["name"], row.get("method_name"), row.get("controller_name")])).lower()
            return sum(1 for t in tokens if t in haystack)

        scored = [(score(r), r) for r in rows]
        rows = [r for s, r in sorted(scored, key=lambda pair: -pair[0]) if s > 0]

    return rows[:limit]


def get_api_context(conn, endpoint_row):
    """endpoint_row: a dict row from the `nodes` table (node_type='ENDPOINT')."""
    endpoint_meta = parse_metadata(endpoint_row)
    method_row = find_exposing_method(conn, endpoint_row["id"])
    controller_row = get_parent_class(conn, method_row["id"]) if method_row else None

    method_meta = parse_metadata(method_row) if method_row else {}
    params = _clean_param_types(method_meta.get("params"))
    return_type = method_meta.get("return_type")

    repos_found, _services_seen, downstream_services = (
        class_dependency_chain(conn, controller_row["id"], endpoint_row.get("service"))
        if controller_row else ([], [], set())
    )
    repo_names = sorted({r["name"] for r in repos_found})
    tables = sorted({inferred_table_name(n) for n in repo_names})

    return prune_empty({
        "service": endpoint_row.get("service"),
        "http_method": endpoint_meta.get("http_method"),
        "path": endpoint_meta.get("path"),
        "controller": controller_row["name"] if controller_row else None,
        "method": method_row["name"] if method_row else None,
        "file": endpoint_row.get("file_path"),
        "parameters": params,
        "request_dto": {"guess": _guess_dto(params), "confidence": "inferred"} if params else None,
        "response_dto": (
            {"guess": return_type, "confidence": "inferred"}
            if return_type and return_type not in _PRIMITIVE_OR_FRAMEWORK_TYPES
            else {"guess": None, "confidence": "unknown"}
        ),
        "repositories": repo_names,
        "tables": tables,
        "downstream_services": sorted(downstream_services),
    })
