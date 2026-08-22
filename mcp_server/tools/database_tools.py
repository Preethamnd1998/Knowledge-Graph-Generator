"""get_database_impact."""
from .. import db
from ..errors import InvalidInputError, NotFoundError
from ..logging_setup import tool_boundary
from ..queries import database_queries


@tool_boundary("get_database_impact")
def get_database_impact(service: str = None, entity: str = None,
                        api: str = None, flow: str = None) -> dict:
    """Change-impact / RCA helper: which tables are read/written/updated/
    deleted by a service, entity, API, or business flow (at least one
    required). Table names and CRUD classification are inferred from
    Repository classes and Spring Data method-naming conventions — the
    indexer doesn't capture real @Table/@Column mappings, so every fact
    here is returned with confidence='inferred', never as verified schema."""
    if not any([service, entity, api, flow]):
        raise InvalidInputError("Provide at least one of: service, entity, api, flow.")
    with db.connect() as conn:
        result = database_queries.get_database_impact(conn, service=service, entity=entity,
                                                        api=api, flow=flow)
    if result is None:
        raise NotFoundError(
            "Could not resolve any repository/table from the given input. Try find_service "
            "or find_api first to confirm names."
        )
    return result


def register(server):
    server.tool()(get_database_impact)
