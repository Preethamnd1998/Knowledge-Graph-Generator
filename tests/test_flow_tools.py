from mcp_server.tools.flow_tools import get_business_flow


def test_get_business_flow_resolves_full_chain():
    flow = get_business_flow(service="orders-service", flow="place order")
    assert "error" not in flow
    roles = [step["role"] for step in flow["chain"]]
    assert roles[0] == "controller"
    assert "repository" in roles
    assert "OrderRepository" in flow["classes"]
    assert "Order" in flow["database_tables"]
    assert "notification-service" in flow["involved_services"]
    assert "notification-service" in flow["external_calls"]


def test_get_business_flow_not_found():
    result = get_business_flow(service="orders-service", flow="totally-unrelated-xyz")
    assert result["error"]["code"] == "not_found"


def test_get_business_flow_unknown_service_via_missing_entry_point():
    result = get_business_flow(service="does-not-exist", flow="anything")
    assert result["error"]["code"] == "not_found"
