from mcp_server.tools.api_tools import find_api, get_api_context


def test_find_api_by_keyword():
    result = find_api(query="orders")
    assert "error" not in result
    paths = [r["path"] for r in result["results"]]
    assert "/orders" in paths


def test_find_api_by_method_and_service():
    result = find_api(method="put", service="orders-service")
    assert len(result["results"]) == 1
    assert result["results"][0]["http_method"] == "PUT"


def test_find_api_requires_at_least_one_filter():
    result = find_api()
    assert result["error"]["code"] == "invalid_input"


def test_get_api_context_full_detail():
    ctx = get_api_context(query="place order", service="orders-service")
    assert ctx["controller"] == "OrderController"
    assert ctx["http_method"] == "POST"
    assert ctx["path"] == "/orders"
    assert "OrderRepository" in ctx["repositories"]
    assert "Order" in ctx["tables"]
    assert "notification-service" in ctx["downstream_services"]
    assert ctx["request_dto"]["confidence"] == "inferred"


def test_get_api_context_not_found():
    result = get_api_context(query="nonexistent-endpoint-xyz")
    assert result["error"]["code"] == "not_found"
