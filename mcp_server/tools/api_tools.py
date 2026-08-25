"""find_api, get_api_context, resolve_ui_api."""
from .. import db
from ..errors import InvalidInputError, NotFoundError
from ..limits import clamp_limit, require_non_empty
from ..logging_setup import tool_boundary
from ..queries import api_queries
from ..queries.common import parse_metadata


def _compact_api(row):
    meta = parse_metadata(row)
    return {
        "service": row.get("service"),
        "http_method": meta.get("http_method"),
        "path": meta.get("path"),
        "controller": row.get("controller_name"),
        "handler": row.get("method_name"),
    }


@tool_boundary("find_api")
def find_api(query: str = None, path: str = None, method: str = None,
             service: str = None, limit: int = None) -> dict:
    """Find REST APIs by keyword, path, HTTP method, and/or service.
    At least one filter is required. Returns concise metadata per match
    (service/method/path/controller/handler) — use get_api_context for
    full detail on one API."""
    if not any([query, path, method, service]):
        raise InvalidInputError("Provide at least one of: query, path, method, service.")
    config = db.get_config()
    limit = clamp_limit(limit, config)
    with db.connect() as conn:
        rows = api_queries.find_apis(conn, query=query, path=path, method=method,
                                      service=service, limit=limit)
    return {"results": [_compact_api(r) for r in rows]}


@tool_boundary("get_api_context")
def get_api_context(query: str, service: str = None) -> dict:
    """Full detail on ONE API: controller/handler, parameters, best-effort
    request/response DTO guesses, repositories and inferred database
    tables it touches, and downstream services it depends on. `query` can
    be an exact 'METHOD /path' or a keyword — the best match is used."""
    if not query or not query.strip():
        raise InvalidInputError("'query' is required.")
    with db.connect() as conn:
        matches = api_queries.find_apis(conn, query=query, service=service, limit=1)
        if not matches:
            # query may be an exact "METHOD /path" name rather than a keyword match
            matches = api_queries.find_apis(conn, path=query, service=service, limit=1)
        if not matches:
            raise NotFoundError(f"No API matching '{query}' found in the knowledge graph.")
        context = api_queries.get_api_context(conn, matches[0])
    return context


@tool_boundary("resolve_ui_api")
def resolve_ui_api(query: str, limit: int = None) -> dict:
    """Resolve a UI API constant (e.g. 'BillingApiConstant.BILLING_CONFIRM_PAYMENT',
    a bare field name, or the literal path like 'ipd/opd/confirm/payment') to
    the backend REST endpoint(s) it calls. Matches are returned even when
    unresolved — check `resolution` for why (e.g. the path's gateway prefix
    doesn't match any indexed backend service, or no endpoint at that path
    was found) rather than assuming a missing endpoints list means no match."""
    query = require_non_empty(query, "query")
    config = db.get_config()
    limit = clamp_limit(limit, config, default=5)
    with db.connect() as conn:
        results = api_queries.find_backend_for_ui_constant(conn, query, limit=limit)
    if not results:
        raise NotFoundError(f"No UI API constant matching '{query}' found in the knowledge graph.")
    return {"results": results}


def register(server):
    server.tool()(find_api)
    server.tool()(get_api_context)
    server.tool()(resolve_ui_api)
