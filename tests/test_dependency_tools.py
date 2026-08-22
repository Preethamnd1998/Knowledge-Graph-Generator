from mcp_server.tools.dependency_tools import find_callers, find_dependencies


def test_find_dependencies_outgoing_on_class():
    result = find_dependencies(target="OrderController", direction="outgoing", depth=1)
    names = [r["name"] for r in result["results"]]
    assert "OrderService" in names


def test_find_dependencies_method_resolves_to_containing_class():
    # OrderController#placeOrder has no outgoing deps of its own; the tool
    # should escalate to OrderController and report ITS dependencies.
    result = find_dependencies(target="placeOrder", direction="outgoing", depth=1)
    assert result["target"] == "OrderController"
    names = [r["name"] for r in result["results"]]
    assert "OrderService" in names


def test_find_callers_on_class():
    result = find_callers(target="OrderService")
    names = [r["name"] for r in result["results"]]
    assert "OrderController" in names


def test_find_dependencies_on_service_uses_service_level_tree():
    result = find_dependencies(target="orders-service", direction="outgoing", depth=1)
    assert result["target_type"] == "service"
    services = [r["service"] for r in result["results"]]
    assert "notification-service" in services


def test_find_dependencies_not_found():
    result = find_dependencies(target="TotallyUnknownXyz")
    assert result["error"]["code"] == "not_found"


def test_circular_dependency_terminates(monkeypatch):
    """ServiceA <-> ServiceB depend on each other — must return, not hang
    or raise a recursion error."""
    result = find_dependencies(target="ServiceA", direction="outgoing", depth=3)
    assert "error" not in result
    names = {r["name"] for r in result["results"]}
    assert "ServiceB" in names
