"""saga_queries._service_dependents must be driven from the project's nodes (idx_nodes_service) so it
cannot degrade into a full DEPENDS_ON edge scan (which intermittently exceeded the KG query budget)."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB
from mcp_server.queries import saga_queries


def _db(tmp_path):
    db = GraphDB(str(tmp_path / "g.db"))
    db.conn.row_factory = sqlite3.Row
    svc = db.add_node("NG_SERVICE", "ChatApiService", qualified_name="ng:p:ChatApiService", service="p", project_type="angular")
    mid = db.add_node("NG_SERVICE", "MidService", qualified_name="ng:p:MidService", service="p", project_type="angular")
    comp = db.add_node("NG_COMPONENT", "ChatComponent", qualified_name="ng:p:ChatComponent", service="p", project_type="angular")
    other = db.add_node("NG_COMPONENT", "OtherComponent", qualified_name="ng:q:OtherComponent", service="q", project_type="angular")
    db.add_edge(mid, "DEPENDS_ON", target_id=svc, service="p")
    db.add_edge(comp, "DEPENDS_ON", target_name="MidService", service="p")   # unresolved-by-id, resolved by name
    db.add_edge(other, "DEPENDS_ON", target_id=svc, service="q")              # same name, other project: must not leak
    db.commit()
    return db


def test_dependents_found_through_two_hops_by_name_within_project(tmp_path):
    db = _db(tmp_path)
    names = {c["name"] for c in saga_queries._service_dependents(db.conn, "p", "ChatApiService")}
    assert names == {"ChatComponent"}


def test_dependents_query_is_driven_from_service_index_not_a_relation_scan(tmp_path):
    db = _db(tmp_path)
    src = open(saga_queries.__file__, encoding="utf-8").read()
    start = src.index("def _service_dependents")
    sql = src[start:src.index("return list(found.values())", start)]
    assert "FROM nodes s CROSS JOIN edges e ON e.source_id = s.id" in sql
    plan = " | ".join(r[3] for r in db.conn.execute(
        "EXPLAIN QUERY PLAN SELECT DISTINCT s.* FROM nodes s CROSS JOIN edges e ON e.source_id = s.id "
        "LEFT JOIN nodes t ON t.id = e.target_id WHERE s.service = ? AND s.node_type IN ('NG_COMPONENT','NG_SERVICE') "
        "AND e.relation = 'DEPENDS_ON' AND (t.name = ? OR e.target_name = ?) LIMIT 12", ("p", "x", "x")))
    assert "idx_nodes_service" in plan and "idx_edges_source" in plan and "idx_edges_relation" not in plan, plan
