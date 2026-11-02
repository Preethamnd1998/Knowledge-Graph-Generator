"""GraphDB.dedupe_edges: exact duplicates go, near-duplicates stay."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB


def test_dedupe_removes_only_exact_duplicates(tmp_path):
    db = GraphDB(str(tmp_path / "d.db"))
    a = db.add_node("CLASS", "A", qualified_name="p.A", service="s", project_type="java")
    b = db.add_node("CLASS", "B", qualified_name="p.B", service="s", project_type="java")
    db.add_edge(a, "DEPENDS_ON", target_id=b, service="s", metadata={"via": "Autowired"})
    db.add_edge(a, "DEPENDS_ON", target_id=b, service="s", metadata={"via": "Autowired"})      # exact dup
    db.add_edge(a, "DEPENDS_ON", target_id=b, service="s", metadata={"via": "Constructor"})    # different metadata: keep
    db.add_edge(a, "IMPORTS", target_name="x.Y", service="s")
    db.add_edge(a, "IMPORTS", target_name="x.Y", service="s")                                   # exact dup, unresolved target
    db.commit()

    assert db.dedupe_edges() == 2
    assert db.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 3
    assert db.dedupe_edges() == 0    # idempotent


def test_kafka_topic_token_unwraps_enum_accessors_but_not_runtime_expressions():
    from kg_indexer.parsers.spring_flow_parser import _normalize_topic_token as n
    assert n("KafkaTopics.LT_IPD_EVENTS") == "LT_IPD_EVENTS"
    assert n("KafkaTopics.LT_IPD_EVENTS.toString()") == "LT_IPD_EVENTS"
    assert n("KafkaTopics.LT_IPD_EVENTS.name()") == "LT_IPD_EVENTS"
    assert n("LT_IPD_EVENTS") == "LT_IPD_EVENTS"
    assert n("topic.toString()") == "toString()"        # a variable: stays an unresolved expression
    assert n("getTopic()") == "getTopic()"


def test_orphaned_edges_from_a_reindexed_target_are_pruned(tmp_path):
    db = GraphDB(str(tmp_path / "o.db"))
    src = db.add_node("API_CONSTANT", "C.X", qualified_name="ng:c", service="UI/x", project_type="angular")
    tgt = db.add_node("ENDPOINT", "GET /a", qualified_name="ep:a", service="BE/s", project_type="java")
    db.add_edge(src, "CALLS_API", target_id=tgt, service="BE/s")
    db.add_edge(src, "USES_API_CONSTANT", target_name="Missing.Y", service="UI/x")   # unresolved but nameable: kept
    # the backend service is re-indexed: its node is deleted and the FK nulls the edge's target
    db.conn.execute("UPDATE edges SET target_id = NULL WHERE target_id = ?", (tgt,))
    db.conn.execute("DELETE FROM nodes WHERE id = ?", (tgt,))
    db.commit()
    assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE relation='CALLS_API' AND target_id IS NULL").fetchone()[0] == 1

    assert db.prune_orphan_edges() == 1
    assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE relation='CALLS_API'").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE relation='USES_API_CONSTANT'").fetchone()[0] == 1
