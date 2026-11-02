"""find_template_events, find_routes — Angular template-event and route
semantic lookups (see queries/angular_queries.py for why these are direct
name/metadata lookups rather than the generic find_dependencies traversal)."""
from .. import db
from ..errors import InvalidInputError
from ..limits import clamp_limit
from ..logging_setup import tool_boundary
from ..queries import angular_queries


@tool_boundary("find_template_events")
def find_template_events(handler: str = None, event: str = None, component: str = None,
                          service: str = None, limit: int = None) -> dict:
    """Find Angular template event bindings ((click)="handler()" and
    friends) by handler method name, event name (click/change/keydown.enter/
    ...), and/or owning component name. At least one filter is required.
    Each result reports handler_kind: 'self_call' (component's own method —
    verify it exists and follow its CALLS chain via Code Cortex/Unified
    Trace, KG has no per-method registry to resolve it further itself),
    'member_call' (an injected service method — member_type is the
    resolved service class), or 'unsupported' (assignment or anything
    deeper than one level of member access — never guessed)."""
    if not any([handler, event, component]):
        raise InvalidInputError("Provide at least one of: handler, event, component.")
    config = db.get_config()
    limit = clamp_limit(limit, config)
    with db.connect() as conn:
        results = angular_queries.find_template_events(
            conn, handler=handler, event=event, component=component, service=service, limit=limit)
    return {"results": results}


@tool_boundary("find_routes")
def find_routes(path: str = None, component: str = None, guard: str = None,
                 resolver: str = None, service: str = None, limit: int = None) -> dict:
    """Find Angular route configuration entries by path (substring match,
    so 'patient' matches 'patient/:id'), target component, a guard class
    in any canActivate/canActivateChild/canDeactivate/canMatch/canLoad
    slot, and/or a resolver class in the resolve map. At least one filter
    is required. `lazy: true` means the route uses loadChildren with no
    direct `component:` — ROUTES_TO isn't created for those (see
    angular_parser.py's route pass — never guessed from the lazy module)."""
    if not any([path, component, guard, resolver]):
        raise InvalidInputError("Provide at least one of: path, component, guard, resolver.")
    config = db.get_config()
    limit = clamp_limit(limit, config)
    with db.connect() as conn:
        results = angular_queries.find_routes(
            conn, path=path, component=component, guard=guard, resolver=resolver,
            service=service, limit=limit)
    return {"results": results}


def register(server):
    server.tool()(find_template_events)
    server.tool()(find_routes)
