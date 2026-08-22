from mcp_server.tools.search_tools import search_knowledge


def test_search_knowledge_prefers_business_flow_over_isolated_nodes():
    result = search_knowledge(query="what happens when an order is placed", limit=5)
    assert result["results"], "expected at least one result"
    top = result["results"][0]
    assert top["type"] == "business_flow"
    assert top["relevance"] == max(r["relevance"] for r in result["results"])


def test_search_knowledge_no_duplicate_names():
    result = search_knowledge(query="order", limit=10)
    seen = set()
    for r in result["results"]:
        key = (r["type"], r["name"])
        assert key not in seen, f"duplicate result: {key}"
        seen.add(key)


def test_search_knowledge_respects_limit():
    result = search_knowledge(query="order service controller", limit=2)
    assert len(result["results"]) <= 2


def test_search_knowledge_empty_query_is_invalid():
    result = search_knowledge(query="   ")
    assert result["error"]["code"] == "invalid_input"
