"""get_business_flow."""
from .. import db
from ..errors import NotFoundError
from ..limits import require_non_empty
from ..logging_setup import tool_boundary
from ..queries import flow_queries


@tool_boundary("get_business_flow")
def get_business_flow(service: str, flow: str) -> dict:
    """Compact end-to-end execution flow for a business operation (e.g.
    flow="cancellation"): the controller -> service -> repository chain,
    involved services, classes, inferred database tables, naming-
    convention-detected events, and downstream/external calls. Derived
    from real DEPENDS_ON edges — there's no dedicated 'flow' node type,
    this is assembled on demand from the closest matching entry point."""
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


def register(server):
    server.tool()(get_business_flow)
