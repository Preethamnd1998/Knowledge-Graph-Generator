"""saga_queries.class_facts: bean-ness, dependencies (incl. constructor injection), supertypes."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB
from mcp_server.queries import saga_queries


def _db(tmp_path):
    db = GraphDB(str(tmp_path / "f.db"))
    svc = "BE/core"
    impl = db.add_node("CLASS", "SettingsServiceImpl", qualified_name="a.SettingsServiceImpl", service=svc,
                       project_type="java", metadata={"annotations": ["Service"]})
    iface = db.add_node("INTERFACE", "CTAService", qualified_name="a.CTAService", service=svc, project_type="java")
    util = db.add_node("CLASS", "AppUtil", qualified_name="a.AppUtil", service=svc, project_type="java",
                       metadata={"annotations": []})
    db.add_edge(impl, "DEPENDS_ON", target_id=iface, service=svc, metadata={"via": "ConstructorInjection"})
    db.add_edge(impl, "DEPENDS_ON", target_name="ProducerService", service=svc, metadata={"via": "Autowired"})
    db.add_edge(impl, "IMPLEMENTS", target_name="SettingsService", service=svc)
    db.commit()
    db.conn.close()
    conn = sqlite3.connect(str(tmp_path / "f.db"))
    conn.row_factory = sqlite3.Row
    return conn


def test_class_facts(tmp_path):
    conn = _db(tmp_path)
    facts = saga_queries.class_facts(conn, ["a.SettingsServiceImpl", "CTAService", "AppUtil", "Missing"], "BE/core")

    impl = facts["SettingsServiceImpl"]
    assert impl["is_bean"] and impl["found"]
    assert impl["depends_on"] == ["CTAService", "ProducerService"]      # resolved AND unresolved targets, simple names
    assert impl["supertypes"] == ["SettingsService"]

    assert facts["CTAService"]["is_bean"]                                # interfaces are injectable
    assert not facts["AppUtil"]["is_bean"]                               # plain utility: not DI-reachable
    assert facts["Missing"] == {"found": False, "is_bean": False, "depends_on": [], "supertypes": []}


def test_method_dep_calls_resolve_dependency_class(tmp_path):
    db = GraphDB(str(tmp_path / "c.db"))
    svc = "BE/core"
    db.add_node("INTERFACE", "CTAService", qualified_name="a.b.CTAService", service=svc, project_type="java")
    m = db.add_node("METHOD", "save", qualified_name="a.b.RuleServiceImpl#save(Rule)", service=svc, project_type="java")
    db.add_edge(m, "CALLS_DEP", target_name="CTAService#getCta", service=svc,
                metadata={"confidence": "verified", "field": "ctaService", "type": "CTAService", "method": "getCta"})
    db.add_edge(m, "CALLS_DEP", target_name="Unknown#x", service=svc,
                metadata={"confidence": "verified", "field": "u", "type": "Unknown", "method": "x"})
    db.commit()
    db.conn.close()
    conn = sqlite3.connect(str(tmp_path / "c.db"))
    conn.row_factory = sqlite3.Row

    out = saga_queries.method_dep_calls(conn, "RuleServiceImpl#save", svc)
    by = {c["type"]: c for c in out["calls"]}
    assert by["CTAService"]["class_fqn"] == "a.b.CTAService" and by["CTAService"]["method"] == "getCta"
    assert by["Unknown"]["class_fqn"] is None            # type not in the graph: reported, not guessed
