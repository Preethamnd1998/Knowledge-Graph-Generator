"""DB_TABLE.engine: a table reached by JPA, raw SQL, or both keeps its engine information
regardless of which access path the parser meets first (add_node returns the existing
node untouched, so the first creator used to win)."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB
from kg_indexer.parsers import spring_flow_parser as sfp
from mcp_server.queries import saga_queries
from tests.test_spring_flow_parser import _index, _write

ENTITY = """
package com.acme.rules.domain;
import javax.persistence.Entity;
import javax.persistence.Table;
@Entity
@Table(name = "auto_follow_up_rule")
public class AutoFollowUpRule { private Long id; }
"""
REPO = """
package com.acme.rules.repo;
import com.acme.rules.domain.AutoFollowUpRule;
import org.springframework.data.jpa.repository.JpaRepository;
public interface AutoFollowUpRuleRepository extends JpaRepository<AutoFollowUpRule, Long> {}
"""
JPA_SVC = """
package com.acme.rules.svc;
import com.acme.rules.repo.AutoFollowUpRuleRepository;
public class ZRuleService {
    private final AutoFollowUpRuleRepository autoFollowUpRuleRepository;
    public void save(Object o) { autoFollowUpRuleRepository.save(o); }
}
"""
SQL_SVC = '''
package com.acme.rules.dao;
public class ARuleDao {
    public Object find() { return jdbc.query("SELECT id FROM auto_follow_up_rule WHERE id = 1"); }
}
'''
SQL_ONLY = '''
package com.acme.rules.dao;
public class ReportDao {
    public Object find() { return jdbc.query("SELECT id FROM report_snapshot WHERE id = 1"); }
}
'''


def _table(db, name):
    r = db.conn.execute("SELECT id, metadata FROM nodes WHERE node_type='DB_TABLE' AND name=?", (name,)).fetchall()
    assert len(r) == 1, f"expected exactly one DB_TABLE node for {name}, got {len(r)}"
    return r[0]["id"], json.loads(r[0]["metadata"])


def _build(tmp_path, files, name="svc"):
    d = tmp_path / name
    for rel, body in files.items():
        _write(d, rel, body)
    return _index(tmp_path, f"{name}.db", name, d)[0]


def _jpa_files(extra=None):
    f = {"src/main/java/com/acme/rules/domain/AutoFollowUpRule.java": ENTITY,
         "src/main/java/com/acme/rules/repo/AutoFollowUpRuleRepository.java": REPO,
         "src/main/java/com/acme/rules/svc/ZRuleService.java": JPA_SVC}
    f.update(extra or {})
    return f


def test_jpa_repository_table_is_engine_jpa(tmp_path):
    db = _build(tmp_path, _jpa_files())
    _, meta = _table(db, "auto_follow_up_rule")
    assert meta["engine"] == "jpa" and meta["engines"] == ["jpa"]


def test_sql_only_table_has_no_engine_key_and_reads_as_sql(tmp_path):
    db = _build(tmp_path, {"src/main/java/com/acme/rules/dao/ReportDao.java": SQL_ONLY})
    _, meta = _table(db, "report_snapshot")
    assert "engine" not in meta and meta["engines"] == ["sql"]


def test_table_seen_by_sql_FIRST_then_jpa_keeps_jpa(tmp_path):
    """ARuleDao sorts/parses before ZRuleService: the original first-writer-wins bug."""
    db = _build(tmp_path, _jpa_files({"src/main/java/com/acme/rules/dao/ARuleDao.java": SQL_SVC}))
    _, meta = _table(db, "auto_follow_up_rule")
    assert meta["engine"] == "jpa" and meta["engines"] == ["jpa", "sql"]


def test_merge_is_order_independent_and_lossless():
    for order in (("sql", "jpa"), ("jpa", "sql"), ("sql", "jpa", "sql"), ("jpa", None)):
        meta = {"confidence": "inferred"}
        for e in order:
            sfp._merge_engine_metadata(meta, e)
        assert meta["engine"] == "jpa" and set(meta["engines"]) == {"jpa", "sql"}, order
    m = sfp._merge_engine_metadata({"confidence": "inferred"}, "mongo")
    sfp._merge_engine_metadata(m, "jpa"); sfp._merge_engine_metadata(m, None)
    assert m["engine"] == "mongo" and m["engines"] == ["jpa", "mongo", "sql"]
    assert m["confidence"] == "inferred"  # unrelated keys survive; nothing nulled


def test_merge_upgrades_legacy_node_that_only_has_engine(tmp_path):
    db = GraphDB(str(tmp_path / "g.db")); db.conn.row_factory = sqlite3.Row
    db.add_node("DB_TABLE", "t", qualified_name="db-table:s:t", service="s", project_type="java",
                metadata={"confidence": "inferred", "engine": "jpa"})
    nid = sfp._get_or_create_table(db, "t", "s")  # sql access to a jpa-tagged node must not erase jpa
    _, meta = _table(db, "t")
    assert meta["engine"] == "jpa" and meta["engines"] == ["jpa", "sql"] and nid


def test_metadata_persists_across_reopen(tmp_path):
    db = _build(tmp_path, _jpa_files({"src/main/java/com/acme/rules/dao/ARuleDao.java": SQL_SVC}))
    db.commit(); db.conn.close()
    again = sqlite3.connect(str(tmp_path / "svc.db")); again.row_factory = sqlite3.Row
    meta = json.loads(again.execute("SELECT metadata FROM nodes WHERE node_type='DB_TABLE' AND name='auto_follow_up_rule'").fetchone()[0])
    assert meta["engine"] == "jpa"


def test_saga_classifies_with_restored_metadata(tmp_path):
    """Same query the saga uses: engine comes from tmeta['engine'] or defaults to 'sql'."""
    db = _build(tmp_path, _jpa_files({"src/main/java/com/acme/rules/dao/ARuleDao.java": SQL_SVC,
                                      "src/main/java/com/acme/rules/dao/ReportDao.java": SQL_ONLY}))
    rows = db.conn.execute("SELECT name, metadata FROM nodes WHERE node_type='DB_TABLE'").fetchall()
    eng = {r["name"]: (saga_queries.parse_metadata({"metadata": r["metadata"]}).get("engine") or "sql") for r in rows}
    assert eng["auto_follow_up_rule"] == "jpa" and eng["report_snapshot"] == "sql"
