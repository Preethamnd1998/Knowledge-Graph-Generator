from . import (
    api_tools, context_pack_tools, database_tools, dependency_tools,
    evidence_tools, flow_tools, search_tools, service_tools,
)

_MODULES = (
    service_tools, api_tools, flow_tools, database_tools,
    dependency_tools, search_tools, evidence_tools, context_pack_tools,
)


def register_all(server):
    for module in _MODULES:
        module.register(server)
