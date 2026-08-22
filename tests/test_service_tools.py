from mcp_server.tools.service_tools import find_service, get_service_context, get_service_dependencies


def test_find_service_matches_by_keyword():
    result = find_service(query="orders")
    assert "error" not in result
    names = [r["name"] for r in result["results"]]
    assert "orders-service" in names


def test_find_service_no_match_returns_empty_not_error():
    result = find_service(query="totally-unknown-xyz")
    assert result["results"] == []


def test_get_service_context_compact_shape():
    ctx = get_service_context(service="orders-service")
    assert ctx["service"] == "orders-service"
    assert ctx["apis_count"] == 2
    assert ctx["entities_count"] == 0
    assert "Spring Boot" in ctx["technology"]
    assert "notification-service" in ctx["dependencies"]
    assert "OrderCreatedEvent" in ctx["events"]
    # Rule 10: no unnecessary empty fields — dependents should be omitted, not [].
    if "dependents" in ctx:
        assert ctx["dependents"] != []


def test_get_service_context_unknown_service_is_not_found():
    result = get_service_context(service="does-not-exist")
    assert result["error"]["code"] == "not_found"


def test_get_service_dependencies_outgoing():
    result = get_service_dependencies(service="orders-service", direction="outgoing", depth=1)
    assert "error" not in result
    services = [d["service"] for d in result["dependencies"]]
    assert "notification-service" in services
    assert all(d["confidence"] == "verified" for d in result["dependencies"])


def test_get_service_dependencies_incoming():
    result = get_service_dependencies(service="notification-service", direction="incoming", depth=1)
    services = [d["service"] for d in result["dependencies"]]
    assert "orders-service" in services


def test_get_service_dependencies_invalid_direction():
    result = get_service_dependencies(service="orders-service", direction="sideways")
    assert result["error"]["code"] == "invalid_input"


def test_get_service_dependencies_depth_is_clamped_not_rejected():
    result = get_service_dependencies(service="orders-service", depth=999)
    assert "error" not in result
    assert result["depth"] == 3  # MCP_MAX_DEPTH set to 3 in conftest
