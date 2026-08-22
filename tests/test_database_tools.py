from mcp_server.tools.database_tools import get_database_impact


def test_get_database_impact_via_api():
    result = get_database_impact(api="place order", service="orders-service")
    assert "error" not in result
    assert result["resolved_via"] == "api"
    assert "Order" in result["tables_read"]
    assert "Order" in result["tables_written"]
    assert "Order" in result["tables_deleted"]
    assert all(f["confidence"] == "inferred" for f in result["functions"])
    assert all(r["confidence"] == "inferred" for r in result["relationships"])


def test_get_database_impact_via_entity():
    result = get_database_impact(entity="Order", service="orders-service")
    assert "OrderRepository" in result["repositories"]


def test_get_database_impact_via_service():
    result = get_database_impact(service="orders-service")
    assert "OrderRepository" in result["repositories"]


def test_get_database_impact_requires_a_parameter():
    result = get_database_impact()
    assert result["error"]["code"] == "invalid_input"


def test_get_database_impact_unresolvable_is_not_found():
    result = get_database_impact(entity="NoSuchEntityXyz", service="orders-service")
    assert result["error"]["code"] == "not_found"
