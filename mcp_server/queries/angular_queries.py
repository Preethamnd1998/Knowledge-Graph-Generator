"""Query logic backing find_template_events / find_routes.

These two are direct, name/metadata-filtered lookups over the
TEMPLATE_EVENT / NG_ROUTE node types angular_parser.py's template-event
and route passes produce — NOT the generic find_dependencies/find_callers
traversal (see common.py's DEPENDENCY_RELATIONS), because that traversal
needs an already-resolved anchor NODE, and a self-form event handler
(`(click)="savePatient()"`) is deliberately left unresolved here (KG has
no per-method registry — see angular_parser.py's HANDLES_EVENT docs). A
caller who only has a bare handler/component/path/guard/resolver NAME
needs a name-first lookup, which is exactly what these two provide.
"""
from ..db import fetch_all, row_to_dict
from .common import parse_metadata


def _compact_event(row):
    meta = parse_metadata(row)
    return {
        "event_name": meta.get("event_name"),
        "handler_expression": meta.get("handler_expression"),
        "handler_kind": meta.get("handler_kind"),
        "method_name": meta.get("method_name"),
        "member_name": meta.get("member_name"),
        "member_type": meta.get("member_type"),
        "component_name": meta.get("component_name"),
        "multi_statement": meta.get("multi_statement", False),
        "service": row.get("service"),
        "file": row.get("file_path"),
        "line": row.get("start_line"),
    }


def find_template_events(conn, handler=None, event=None, component=None, service=None, limit=10):
    """Find TEMPLATE_EVENT nodes by handler method name, event name
    (click/change/keydown.enter/...), and/or owning component name.
    At least one filter is required by the caller (find_template_events
    tool layer enforces this)."""
    sql = "SELECT * FROM nodes WHERE node_type = 'TEMPLATE_EVENT'"
    params = []
    if handler:
        sql += (" AND (json_extract(metadata, '$.method_name') = ? "
                "OR json_extract(metadata, '$.handler_expression') LIKE ?)")
        params.extend([handler, f"%{handler}%"])
    if event:
        sql += " AND json_extract(metadata, '$.event_name') = ?"
        params.append(event)
    if component:
        sql += " AND json_extract(metadata, '$.component_name') = ?"
        params.append(component)
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " LIMIT ?"
    params.append(limit)
    rows = [row_to_dict(r) for r in fetch_all(conn, sql, params)]
    return [_compact_event(r) for r in rows]


def _compact_route(row):
    meta = parse_metadata(row)
    return {
        "path": meta.get("path"),
        "component": meta.get("component_name"),
        "lazy": meta.get("lazy", False),
        "lazy_module_path": meta.get("lazy_module_path"),
        "lazy_module_symbol": meta.get("lazy_module_symbol"),
        "guards": meta.get("guards") or {},
        "resolvers": meta.get("resolvers") or {},
        "service": row.get("service"),
        "file": row.get("file_path"),
        "line": row.get("start_line"),
    }


def find_routes(conn, path=None, component=None, guard=None, resolver=None, service=None, limit=10):
    """Find NG_ROUTE nodes by path, target component, a guard class
    appearing in ANY canActivate*/canMatch/canLoad slot, and/or a
    resolver class appearing in the resolve map."""
    sql = "SELECT * FROM nodes WHERE node_type = 'NG_ROUTE'"
    params = []
    if path:
        sql += " AND json_extract(metadata, '$.path') LIKE ?"
        params.append(f"%{path}%")
    if component:
        sql += " AND json_extract(metadata, '$.component_name') = ?"
        params.append(component)
    if guard:
        sql += " AND json_extract(metadata, '$.guards') LIKE ?"
        params.append(f"%{guard}%")
    if resolver:
        sql += " AND json_extract(metadata, '$.resolvers') LIKE ?"
        params.append(f"%{resolver}%")
    if service:
        sql += " AND service = ?"
        params.append(service)
    sql += " LIMIT ?"
    params.append(limit)
    rows = [row_to_dict(r) for r in fetch_all(conn, sql, params)]
    return [_compact_route(r) for r in rows]
