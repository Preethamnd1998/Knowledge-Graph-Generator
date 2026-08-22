from mcp_server.tools.evidence_tools import get_source_evidence


def test_get_source_evidence_for_class():
    ev = get_source_evidence(node="OrderController")
    assert ev["node_type"] == "CLASS"
    assert ev["file"].endswith("OrderController.java")
    assert ev["line_start"] == 10
    assert ev["line_end"] == 60
    assert "signature" in ev
    assert ev["method"] is None


def test_get_source_evidence_for_method_includes_containing_class():
    ev = get_source_evidence(node="placeOrder")
    assert ev["node_type"] == "METHOD"
    assert ev["class"] == "OrderController"
    assert ev["method"] == "placeOrder"


def test_get_source_evidence_does_not_return_full_file():
    ev = get_source_evidence(node="OrderController")
    # Evidence is a pointer (file/line/signature), never the file's full text.
    assert "content" not in ev
    assert "source_code" not in ev
    assert len(ev.get("signature") or "") < 1000


def test_get_source_evidence_not_found():
    result = get_source_evidence(node="TotallyUnknownXyz")
    assert result["error"]["code"] == "not_found"
