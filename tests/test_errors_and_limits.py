import pytest

from mcp_server.tools.service_tools import find_service, get_service_context
from mcp_server.tools.dependency_tools import find_dependencies


def test_missing_database_returns_clean_error(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_DB_PATH", str(tmp_path / "does-not-exist.db"))
    result = get_service_context(service="orders-service")
    assert result["error"]["code"] == "db_not_found"
    # Never leak the raw path-construction exception text/stack.
    assert "Traceback" not in str(result)


def test_empty_query_is_invalid_input():
    result = find_service(query="")
    assert result["error"]["code"] == "invalid_input"


def test_non_integer_depth_is_invalid_input():
    result = find_dependencies(target="OrderController", depth="not-a-number")
    assert result["error"]["code"] == "invalid_input"


def test_result_limit_is_never_exceeded(monkeypatch):
    monkeypatch.setenv("MCP_MAX_RESULTS", "2")
    result = find_service(query="o")  # broad query, would match many without the cap
    assert len(result["results"]) <= 2


def test_requested_limit_above_ceiling_is_clamped(monkeypatch):
    monkeypatch.setenv("MCP_MAX_RESULTS", "3")
    result = find_service(query="o", limit=1000)
    assert len(result["results"]) <= 3
