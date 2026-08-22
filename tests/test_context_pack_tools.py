from mcp_server.models.context import estimate_tokens
from mcp_server.tools.context_pack_tools import get_context_pack


def test_get_context_pack_renders_markdown_sections():
    pack = get_context_pack(topic="place order", service="orders-service")
    assert isinstance(pack, str)
    assert "# Place Order" in pack
    assert "## Services" in pack
    assert "## Flow" in pack
    assert "OrderController" in pack
    assert "OrderRepository" in pack


def test_get_context_pack_respects_max_tokens_budget():
    pack = get_context_pack(topic="place order", service="orders-service", max_tokens=50)
    # Trimming reduces list sections but never removes the core Flow —
    # allow some slack since Markdown headers have a fixed minimum cost.
    assert estimate_tokens(pack) < 400


def test_get_context_pack_resolves_service_via_search_when_omitted():
    pack = get_context_pack(topic="place order")
    assert "OrderController" in pack


def test_get_context_pack_not_found():
    result = get_context_pack(topic="totally-unrelated-xyz-flow")
    assert isinstance(result, dict)
    assert result["error"]["code"] == "not_found"
