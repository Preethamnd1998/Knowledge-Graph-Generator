"""Repository-based persistence edges (spring_flow_parser._build_mongo_model/_scan_mongo).

Synthetic files only, mirroring the real patterns found in the services:
Mongo repository CRUD, MongoTemplate with Entity.class or a literal
collection, JPA repositories (default snake_case table / explicit @Table),
and schema-qualified SQL strings — with a guard that JPA repositories are
never mislabelled as Mongo.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_spring_flow_parser import _index, _write

ENTITY = """
package com.acme.clin.domain;

import org.springframework.data.mongodb.core.mapping.Document;

@Document(collection = "clinical_event_data_store")
public class ClinicalEvent {
    private String id;
}
"""

DEFAULT_NAME_ENTITY = """
package com.acme.clin.domain;

import org.springframework.data.mongodb.core.mapping.Document;

@Document
public class PatientNote {
    private String id;
}
"""

REPO = """
package com.acme.clin.repo;

import com.acme.clin.domain.ClinicalEvent;
import org.springframework.data.mongodb.repository.MongoRepository;

public interface ClinicalEventRepository extends MongoRepository<ClinicalEvent, String> {
    java.util.List<ClinicalEvent> findByPatientId(String patientId);
}
"""

NOTE_REPO = """
package com.acme.clin.repo;

import com.acme.clin.domain.PatientNote;
import org.springframework.data.mongodb.repository.MongoRepository;

public interface PatientNoteRepository extends MongoRepository<PatientNote, String> {
}
"""

SERVICE = """
package com.acme.clin.service;

import com.acme.clin.domain.ClinicalEvent;
import com.acme.clin.repo.ClinicalEventRepository;
import com.acme.clin.repo.PatientNoteRepository;
import org.springframework.data.mongodb.core.MongoTemplate;
import org.springframework.stereotype.Service;

@Service
public class ClinicalService {

    private final ClinicalEventRepository eventRepository;
    private final PatientNoteRepository noteRepository;
    private final MongoTemplate mongoTemplate;

    public void record(ClinicalEvent e) {
        eventRepository.save(e);
    }

    public void load(String id) {
        eventRepository.findByPatientId(id);
        noteRepository.findAll();
    }

    public void purge(String id) {
        eventRepository.deleteById(id);
    }

    public void patch(Query q, Update u) {
        mongoTemplate.updateFirst(q, u, ClinicalEvent.class);
    }

    public void patchByName(Query q, Update u) {
        mongoTemplate.updateMulti(q, u, "legacy_audit");
    }

    public void unknownTarget(Object o) {
        mongoTemplate.save(o);
    }

    public void notMongo() {
        eventRepository.toString();
    }

    public void queryFieldIsNotACollection(String id) {
        mongoTemplate.updateFirst(Query.query(Criteria.where("_id").is(id)), new Update().set("a", 1), Unmapped.class);
    }
}
"""

JPA_ENTITY = """
package com.acme.bill.domain;

import javax.persistence.Entity;

@Entity
public class InvoiceHeader {
    private Long id;
}
"""

JPA_TABLE_ENTITY = """
package com.acme.bill.domain;

import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "pay_tbl", schema = "public")
public class Payment {
    private Long id;
}
"""

JPA_REPO = """
package com.acme.bill.repo;

import com.acme.bill.domain.InvoiceHeader;
import org.springframework.data.repository.CrudRepository;

public interface InvoiceRepository extends CrudRepository<InvoiceHeader, Long> {
}
"""

JPA_PAY_REPO = """
package com.acme.bill.repo;

import com.acme.bill.domain.Payment;
import org.springframework.data.jpa.repository.JpaRepository;

public interface PaymentRepository extends JpaRepository<Payment, Long> {
}
"""

JPA_SERVICE = """
package com.acme.bill.service;

import com.acme.bill.repo.InvoiceRepository;
import com.acme.bill.repo.PaymentRepository;

public class BillingService {
    private final InvoiceRepository invoiceRepository;
    private final PaymentRepository paymentRepository;

    public void pay(Object o) {
        invoiceRepository.save(o);
        paymentRepository.saveAndFlush(o);
    }

    public void lookup(Long id) {
        invoiceRepository.findById(id);
    }
}
"""


SCHEMA_SQL_SERVICE = '''
package com.acme.bill.repo;

public class SerialRepository {
    public void add(Object o) {
        jdbc.update("INSERT INTO public.serial_number (id) VALUES (?)", o);
    }
    public void bump() {
        jdbc.update("UPDATE billing.serial_number SET n = n + 1 WHERE id = 1");
    }
    public Object find() {
        return jdbc.query("SELECT id FROM public.serial_number WHERE id = 1");
    }
}
'''


SPLIT_LITERAL_SQL_SERVICE = '''
package com.acme.rules.repo;

public class RuleRepository {
    public Object builder(Long id) {
        StringBuilder sql = new StringBuilder();
        sql.append("SELECT r.id, r.name, ");
        sql.append("(SELECT count(*) FROM audit_rule a WHERE a.rid = r.id) AS n ");
        sql.append("FROM auto_follow_up_rule r ");
        sql.append("WHERE r.active = true");
        return jdbc.query(sql.toString(), mapper);
    }
    public void concat() {
        String q = "UPDATE rule_settings " +
                   "SET enabled = ? " +
                   "WHERE id = ?";
        jdbc.update(q);
    }
    public void logOnly() {
        LOGGER.error("Could not select item");
        LOGGER.error("data from the queue");
    }
}
'''


def _edges(db, relation):
    rows = db.conn.execute(
        """SELECT s.qualified_name AS src, t.name AS table_name, e.metadata AS meta, e.target_name AS tname
           FROM edges e JOIN nodes s ON s.id = e.source_id LEFT JOIN nodes t ON t.id = e.target_id
           WHERE e.relation = ?""", (relation,)).fetchall()
    import json
    return [(r["src"].split("#")[-1].split("(")[0], r["table_name"], json.loads(r["meta"] or "{}"), r["tname"])
            for r in rows]


def test_mongo_repository_and_template_edges(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/clin/domain/ClinicalEvent.java", ENTITY)
    _write(svc, "src/main/java/com/acme/clin/domain/PatientNote.java", DEFAULT_NAME_ENTITY)
    _write(svc, "src/main/java/com/acme/clin/repo/ClinicalEventRepository.java", REPO)
    _write(svc, "src/main/java/com/acme/clin/repo/PatientNoteRepository.java", NOTE_REPO)
    _write(svc, "src/main/java/com/acme/clin/service/ClinicalService.java", SERVICE)
    db, _ = _index(tmp_path, "mongo.db", "BE/clin", svc)

    writes = {(m, t, meta["operation"], meta["engine"]) for m, t, meta, _ in _edges(db, "WRITES") if t}
    reads = {(m, t, meta["operation"]) for m, t, meta, _ in _edges(db, "READS") if t}

    assert ("record", "clinical_event_data_store", "save", "mongo") in writes
    assert ("purge", "clinical_event_data_store", "delete", "mongo") in writes
    assert ("patch", "clinical_event_data_store", "update", "mongo") in writes      # Entity.class arg
    assert ("patchByName", "legacy_audit", "update", "mongo") in writes             # literal collection
    assert ("load", "clinical_event_data_store", "select") in reads                 # derived query method
    assert ("load", "patientNote", "select") in reads                               # @Document default name
    assert not any(m == "notMongo" for m, *_ in writes | {(m, t, o) for m, t, o in reads})  # toString: not CRUD


def test_template_call_with_unresolvable_collection_stays_unknown(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/clin/domain/ClinicalEvent.java", ENTITY)
    _write(svc, "src/main/java/com/acme/clin/repo/ClinicalEventRepository.java", REPO)
    _write(svc, "src/main/java/com/acme/clin/service/ClinicalService.java", SERVICE)
    db, _ = _index(tmp_path, "mongo2.db", "BE/clin", svc)

    unresolved = [(m, meta["confidence"], meta["via"]) for m, t, meta, tname in _edges(db, "WRITES") if t is None]
    assert ("unknownTarget", "unknown", "MongoTemplate.save") in unresolved
    # a string literal inside the query criteria ("_id") is a field name, never a collection
    assert ("queryFieldIsNotACollection", "unknown", "MongoTemplate.updateFirst") in unresolved
    assert not any(t == "_id" for _, t, _, _ in _edges(db, "WRITES"))


def test_schema_qualified_sql_yields_table_not_schema(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/bill/repo/SerialRepository.java", SCHEMA_SQL_SERVICE)
    db, _ = _index(tmp_path, "schema.db", "BE/bill", svc)

    tables = {t for _, t, _, _ in _edges(db, "WRITES") + _edges(db, "READS") if t}
    assert tables == {"serial_number"}   # never 'public' / 'billing'


def test_sql_split_across_string_literals_is_reassembled_but_logs_are_not(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/rules/repo/RuleRepository.java", SPLIT_LITERAL_SQL_SERVICE)
    db, _ = _index(tmp_path, "split.db", "BE/rules", svc)

    reads = {(m, t) for m, t, _, _ in _edges(db, "READS") if t}
    writes = {(m, t) for m, t, _, _ in _edges(db, "WRITES") if t}
    assert ("builder", "auto_follow_up_rule") in reads      # StringBuilder.append chain
    assert ("concat", "rule_settings") in writes            # "a" + "b" concatenation
    assert not any(m == "logOnly" for m, _ in reads | writes)   # unrelated literals are never joined


def test_jpa_repositories_resolve_to_tables_and_are_not_labelled_mongo(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/bill/domain/InvoiceHeader.java", JPA_ENTITY)
    _write(svc, "src/main/java/com/acme/bill/domain/Payment.java", JPA_TABLE_ENTITY)
    _write(svc, "src/main/java/com/acme/bill/repo/InvoiceRepository.java", JPA_REPO)
    _write(svc, "src/main/java/com/acme/bill/repo/PaymentRepository.java", JPA_PAY_REPO)
    _write(svc, "src/main/java/com/acme/bill/service/BillingService.java", JPA_SERVICE)
    db, _ = _index(tmp_path, "jpa.db", "BE/bill", svc)

    writes = {(m, t, meta["engine"], meta["operation"]) for m, t, meta, _ in _edges(db, "WRITES") if t}
    reads = {(m, t, meta["engine"]) for m, t, meta, _ in _edges(db, "READS") if t}
    assert ("pay", "invoice_header", "jpa", "save") in writes     # default snake_case naming
    assert ("pay", "pay_tbl", "jpa", "save") in writes            # explicit @Table(name=...), schema ignored
    assert ("lookup", "invoice_header", "jpa") in reads
    assert not any(engine == "mongo" for _, _, engine, _ in writes)


def test_template_call_on_a_document_without_at_document_uses_the_default_collection(tmp_path):
    """`readMongoTemplate.count(q, ClinicalSchedule.class)` where ClinicalSchedule is a Mongo class with
    only @CompoundIndexes (no @Document), plus a static helper handed the template."""
    svc = tmp_path / "ccs"
    _write(svc, "src/main/java/com/acme/ccs/ClinicalSchedule.java", """
package com.acme.ccs;
import org.springframework.data.mongodb.core.index.CompoundIndexes;
import org.springframework.data.mongodb.core.index.CompoundIndex;
@CompoundIndexes({ @CompoundIndex(name = "i", def = "{'a': 1}") })
public class ClinicalSchedule { }
""")
    _write(svc, "src/main/java/com/acme/ccs/PlainDto.java", "package com.acme.ccs;\npublic class PlainDto { }\n")
    _write(svc, "src/main/java/com/acme/ccs/ConductQService.java", """
package com.acme.ccs;
import org.springframework.data.mongodb.core.MongoTemplate;
import org.springframework.stereotype.Service;
@Service
public class ConductQService {
    @Autowired
    @Qualifier("readMongoTemplate")
    MongoTemplate readMongoTemplate;

    public long queue(Query q) {
        long total = this.readMongoTemplate.count(q, ClinicalSchedule.class);
        java.util.List<ClinicalSchedule> rows = CSAppUtil.boundedFind(this.readMongoTemplate, q, ClinicalSchedule.class);
        java.util.List<PlainDto> dtos = CSAppUtil.boundedFind(readMongoTemplate, q, PlainDto.class);
        return total;
    }
}
""")
    db, _ = _index(tmp_path, "ccs.db", "ccs", svc)
    rows = db.conn.execute(
        """SELECT e.relation, COALESCE(t.name, e.target_name), e.metadata FROM edges e
           JOIN nodes s ON s.id = e.source_id LEFT JOIN nodes t ON t.id = e.target_id
           WHERE s.name = 'queue' AND e.relation IN ('READS','WRITES')""").fetchall()
    got = {(r[0], r[1]) for r in rows}
    assert ("READS", "clinicalSchedule") in got
    vias = {json.loads(r[2]).get("via") for r in rows}
    assert "MongoTemplate.count" in vias and "CSAppUtil.boundedFind" in vias
    # PlainDto is not a persisted class: never invent a collection for it
    assert not any(n == "plainDto" for _, n in got)


def test_document_annotation_survives_comments_before_other_class_annotations(tmp_path):
    """The real ClinicalSchedule: `@Document(collection="ccs_clinical_schedule")`, then `//` comment lines,
    then `@CompoundIndexes(..)`. The comment used to cut the annotation run, dropping @Document and
    leaving the wrong default collection name."""
    svc = tmp_path / "ccs"
    _write(svc, "src/main/java/com/acme/ccs/ClinicalSchedule.java", """
package com.acme.ccs;
import org.springframework.data.mongodb.core.mapping.Document;
import org.springframework.data.mongodb.core.index.CompoundIndexes;
import org.springframework.data.mongodb.core.index.CompoundIndex;
@Document(collection = "ccs_clinical_schedule")
// RC-1 fix: filtered by {customerBusinessId, siteId}; mentions @Document(collection = "not_this")
/* block comment
   between annotations */
@CompoundIndexes({ @CompoundIndex(name = "i", def = "{'a': 1}"),
    // Equality (always present, .is() or .in()) - a comment nested three parens deep
    // inside the annotation, url "http://not-a-comment" stays in a string
    @CompoundIndex(name = "j", def = "{'b': 1}") })
public class ClinicalSchedule { }
""")
    _write(svc, "src/main/java/com/acme/ccs/ConductQService.java", """
package com.acme.ccs;
import org.springframework.data.mongodb.core.MongoTemplate;
import org.springframework.stereotype.Service;
@Service
public class ConductQService {
    MongoTemplate readMongoTemplate;
    public long queue(Query q) {
        return this.readMongoTemplate.count(q, ClinicalSchedule.class);
    }
}
""")
    db, _ = _index(tmp_path, "ccs.db", "ccs", svc)
    rows = db.conn.execute(
        """SELECT COALESCE(t.name, e.target_name) FROM edges e
           JOIN nodes s ON s.id = e.source_id LEFT JOIN nodes t ON t.id = e.target_id
           WHERE s.name = 'queue' AND e.relation = 'READS'""").fetchall()
    assert {r[0] for r in rows} == {"ccs_clinical_schedule"}
