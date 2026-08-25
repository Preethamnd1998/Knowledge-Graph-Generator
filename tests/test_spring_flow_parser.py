"""Unit tests for kg_indexer/parsers/spring_flow_parser.py.

Uses small synthetic .java files (standard unit-test practice) — this is
distinct from the mandatory real-workspace Price Book acceptance check,
which must run against the actual UI-BE source and is exercised manually
per MCP.md/AGENTS.md convention, not as a pytest here (no real customer
DB should be required to run `pytest`).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from kg_indexer.db import GraphDB
from kg_indexer.parsers.common import NameRegistry
from kg_indexer.parsers import java_parser, spring_flow_parser


def _write(service_dir, rel_path, content):
    full = service_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


def _index(tmp_path, db_name, service_name, service_dir):
    db = GraphDB(str(tmp_path / db_name))
    db.conn.row_factory = sqlite3.Row
    registry = NameRegistry()
    java_parser.parse_service(db, registry, service_name, str(service_dir))
    flow_stats = spring_flow_parser.parse_service(db, registry, service_name, str(service_dir))
    db.resolve_exact_import_edges("java")
    db.resolve_pending_edges(registry.as_dict(), project_type="java", exclude_relations=("IMPORTS",))
    db.commit()
    return db, flow_stats


BATCH_CONFIG_JAVA = """
package com.acme.orders.batch;

import org.springframework.batch.core.Job;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.job.builder.JobBuilder;
import org.springframework.batch.core.step.builder.StepBuilder;
import org.springframework.batch.core.step.tasklet.Tasklet;
import org.springframework.batch.core.configuration.annotation.StepScope;
import org.springframework.batch.repeat.RepeatStatus;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class OrderBatchConfig {

	@Autowired
	private JobRepository jobRepository;

	@Bean
	public Job exportOrdersJob() {
		return new JobBuilder("exportOrdersJob", this.jobRepository).start(exportOrdersStep()).build();
	}

	@Bean
	public Step exportOrdersStep() {
		return new StepBuilder("exportOrdersStep", this.jobRepository)
				.tasklet(exportOrdersTasklet(null), this.transactionManager).build();
	}

	@Bean
	@StepScope
	public Tasklet exportOrdersTasklet(@Value("#{jobParameters['batchId']}") Long batchId) {
		return (contribution, chunkContext) -> {
			return RepeatStatus.FINISHED;
		};
	}
}
"""

SERVICE_ASYNC_JAVA = """
package com.acme.orders.service;

import java.util.concurrent.CompletableFuture;
import org.springframework.batch.core.Job;
import org.springframework.batch.core.JobParametersBuilder;
import org.springframework.batch.core.launch.JobLauncher;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

@Service
public class OrderExportServiceImpl {

	@Autowired
	private JobLauncher jobLauncher;

	@Autowired
	@Qualifier("exportOrdersJob")
	private Job exportOrdersJob;

	public void triggerExport() {
		CompletableFuture.runAsync(() -> {
			try {
				this.jobLauncher.run(this.exportOrdersJob, new JobParametersBuilder().toJobParameters());
			} catch (Exception e) {
			}
		});
	}

	@Async
	public void notifyAsync() {
	}
}
"""

PRODUCER_JAVA = """
package com.acme.orders.repository;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import com.acme.orders.events.ProducerService;
import com.acme.orders.common.OrderTopics;

@Repository
public class OrderRepository {

	@Autowired
	private JdbcTemplate jdbcTemplate;

	@Autowired
	private ProducerService producerService;

	public void bulkInsert() {
		this.jdbcTemplate.update("INSERT INTO orders (id, name) VALUES (?, ?)", 1, "x");
	}

	public void removeOld() {
		this.jdbcTemplate.update("DELETE FROM orders WHERE id = ?", 1);
	}

	public java.util.List<Object> jpqlRead() {
		return this.entityManager.createQuery("select o from Order o where o.id = :id").getResultList();
	}

	private void publishCreated() {
		this.producerService.publishToKafka(1, OrderTopics.ORDER_CREATED, "{}", null);
	}
}
"""

CONSUMER_JAVA = """
package com.acme.billing.consumer;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;
import com.acme.orders.common.OrderTopics;

@Component
public class OrderCreatedConsumer {

	@KafkaListener(topics = OrderTopics.ORDER_CREATED)
	public void onOrderCreated(String message) {
	}
}
"""


@pytest.fixture
def orders_service(tmp_path):
    service_dir = tmp_path / "orders-service" / "src"
    _write(service_dir, "batch/OrderBatchConfig.java", BATCH_CONFIG_JAVA)
    _write(service_dir, "service/OrderExportServiceImpl.java", SERVICE_ASYNC_JAVA)
    _write(service_dir, "repository/OrderRepository.java", PRODUCER_JAVA)
    return service_dir


def test_batch_job_step_tasklet_chain(tmp_path, orders_service):
    db, stats = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn

    job = conn.execute("SELECT * FROM nodes WHERE node_type='BATCH_JOB' AND name='exportOrdersJob'").fetchone()
    assert job is not None

    step_edge = conn.execute(
        "SELECT * FROM edges WHERE source_id=? AND relation='HAS_STEP'", (job["id"],)
    ).fetchone()
    assert step_edge is not None
    assert step_edge["target_id"] is not None, "Step should resolve to a real BATCH_STEP node"

    step = conn.execute("SELECT * FROM nodes WHERE id=?", (step_edge["target_id"],)).fetchone()
    assert step["node_type"] == "BATCH_STEP"
    assert step["name"] == "exportOrdersStep"

    tasklet_edge = conn.execute(
        "SELECT * FROM edges WHERE source_id=? AND relation='USES_TASKLET'", (step["id"],)
    ).fetchone()
    assert tasklet_edge is not None
    assert tasklet_edge["target_id"] is not None
    tasklet = conn.execute("SELECT * FROM nodes WHERE id=?", (tasklet_edge["target_id"],)).fetchone()
    assert tasklet["node_type"] == "BATCH_TASKLET"
    assert tasklet["name"] == "exportOrdersTasklet"


def test_configuration_defines_bean_edge(tmp_path, orders_service):
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn
    config_class = conn.execute("SELECT * FROM nodes WHERE node_type='CLASS' AND name='OrderBatchConfig'").fetchone()
    job = conn.execute("SELECT * FROM nodes WHERE node_type='BATCH_JOB'").fetchone()
    edge = conn.execute(
        "SELECT * FROM edges WHERE source_id=? AND target_id=? AND relation='DEFINES_BEAN'",
        (config_class["id"], job["id"]),
    ).fetchone()
    assert edge is not None
    import json
    assert json.loads(edge["metadata"])["confidence"] == "verified"


def test_async_annotation_creates_operation(tmp_path, orders_service):
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn
    row = conn.execute("SELECT * FROM nodes WHERE node_type='ASYNC_OPERATION' AND name='notifyAsync'").fetchone()
    assert row is not None


def test_completable_future_run_async_creates_operation_and_edge(tmp_path, orders_service):
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn
    method = conn.execute("SELECT * FROM nodes WHERE node_type='METHOD' AND name='triggerExport'").fetchone()
    assert method is not None
    edge = conn.execute(
        "SELECT * FROM edges WHERE source_id=? AND relation='ASYNC_CALL'", (method["id"],)
    ).fetchone()
    assert edge is not None
    target = conn.execute("SELECT * FROM nodes WHERE id=?", (edge["target_id"],)).fetchone()
    assert target["node_type"] == "ASYNC_OPERATION"


def test_joblauncher_run_resolves_via_qualifier(tmp_path, orders_service):
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn
    method = conn.execute("SELECT * FROM nodes WHERE node_type='METHOD' AND name='triggerExport'").fetchone()
    job = conn.execute("SELECT * FROM nodes WHERE node_type='BATCH_JOB' AND name='exportOrdersJob'").fetchone()
    edge = conn.execute(
        "SELECT * FROM edges WHERE source_id=? AND relation='RUNS'", (method["id"],)
    ).fetchone()
    assert edge is not None
    assert edge["target_id"] == job["id"], "RUNS should resolve through the @Qualifier field to the real Job bean"


def test_kafka_producer_and_topic_writes_reads(tmp_path, orders_service):
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn

    topic = conn.execute("SELECT * FROM nodes WHERE node_type='KAFKA_TOPIC' AND name='ORDER_CREATED'").fetchone()
    assert topic is not None

    publish_edge = conn.execute(
        "SELECT * FROM edges WHERE relation='PUBLISHES' AND target_id=?", (topic["id"],)
    ).fetchone()
    assert publish_edge is not None

    insert_table = conn.execute("SELECT * FROM nodes WHERE node_type='DB_TABLE' AND name='orders'").fetchone()
    assert insert_table is not None
    writes = conn.execute(
        "SELECT * FROM edges WHERE relation='WRITES' AND target_id=?", (insert_table["id"],)
    ).fetchall()
    assert len(writes) >= 2  # INSERT + DELETE both hit the same table

    # The JPQL "select o from Order o" must NOT create a DB_TABLE named
    # "Order"/"order" — PascalCase JPQL entity refs are excluded on purpose.
    jpql_leak = conn.execute("SELECT * FROM nodes WHERE node_type='DB_TABLE' AND name IN ('Order','order')").fetchone()
    assert jpql_leak is None


def test_no_duplicate_table_nodes_for_repeated_table_name(tmp_path, orders_service):
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn
    count = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_type='DB_TABLE' AND name='orders'").fetchone()[0]
    assert count == 1, "INSERT and DELETE against the same table must resolve to one node, not two"


def test_kafka_listener_consumer(tmp_path):
    service_dir = tmp_path / "billing-service" / "src"
    _write(service_dir, "consumer/OrderCreatedConsumer.java", CONSUMER_JAVA)
    db, _ = _index(tmp_path, "billing.db", "billing-service", service_dir)
    conn = db.conn

    topic = conn.execute("SELECT * FROM nodes WHERE node_type='KAFKA_TOPIC' AND name='ORDER_CREATED'").fetchone()
    assert topic is not None
    consume_edge = conn.execute(
        "SELECT * FROM edges WHERE relation='CONSUMES' AND target_id=?", (topic["id"],)
    ).fetchone()
    assert consume_edge is not None
    import json
    assert json.loads(consume_edge["metadata"])["confidence"] == "verified"


def test_cross_service_topic_identity_is_shared(tmp_path, orders_service):
    """A producer in orders-service and a consumer in billing-service
    referencing the same topic constant name must resolve to the SAME
    KAFKA_TOPIC node — topics are never scoped to one service."""
    db = GraphDB(str(tmp_path / "combined.db"))
    db.conn.row_factory = sqlite3.Row
    registry = NameRegistry()

    java_parser.parse_service(db, registry, "orders-service", str(orders_service))
    spring_flow_parser.parse_service(db, registry, "orders-service", str(orders_service))

    billing_dir = tmp_path / "billing-service" / "src"
    _write(billing_dir, "consumer/OrderCreatedConsumer.java", CONSUMER_JAVA)
    java_parser.parse_service(db, registry, "billing-service", str(billing_dir))
    spring_flow_parser.parse_service(db, registry, "billing-service", str(billing_dir))

    conn = db.conn
    topics = conn.execute("SELECT * FROM nodes WHERE node_type='KAFKA_TOPIC' AND name='ORDER_CREATED'").fetchall()
    assert len(topics) == 1, "the same topic constant name must dedupe to one node across services"

    topic_id = topics[0]["id"]
    producers = conn.execute("SELECT * FROM edges WHERE relation='PUBLISHES' AND target_id=?", (topic_id,)).fetchall()
    consumers = conn.execute("SELECT * FROM edges WHERE relation='CONSUMES' AND target_id=?", (topic_id,)).fetchall()
    assert len(producers) == 1
    assert len(consumers) == 1


def test_unresolvable_job_reference_stays_unknown_not_guessed(tmp_path):
    """jobLauncher.run() referencing a job name that doesn't match any
    known BATCH_JOB bean must stay unresolved (target_name only,
    confidence unknown) — never silently linked to an unrelated node."""
    service_dir = tmp_path / "lonely-service" / "src"
    _write(service_dir, "service/LonelyServiceImpl.java", """
package com.acme.lonely;

import org.springframework.batch.core.launch.JobLauncher;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

@Service
public class LonelyServiceImpl {

	@Autowired
	private JobLauncher jobLauncher;

	public void run() {
		this.jobLauncher.run(this.someUnknownJob, null);
	}
}
""")
    db, _ = _index(tmp_path, "lonely.db", "lonely-service", service_dir)
    conn = db.conn
    import json
    edge = conn.execute("SELECT * FROM edges WHERE relation='RUNS'").fetchone()
    assert edge is not None
    assert edge["target_id"] is None
    assert edge["target_name"] == "someUnknownJob"
    assert json.loads(edge["metadata"])["confidence"] == "inferred"


def test_regression_existing_class_and_method_nodes_unaffected(tmp_path, orders_service):
    """The new pass must never change what java_parser itself produces —
    same classes, same methods, same REST-endpoint-style behavior."""
    db, _ = _index(tmp_path, "orders.db", "orders-service", orders_service)
    conn = db.conn
    class_names = {r["name"] for r in conn.execute("SELECT name FROM nodes WHERE node_type='CLASS'")}
    assert {"OrderBatchConfig", "OrderExportServiceImpl", "OrderRepository"} <= class_names

    method_names = {r["name"] for r in conn.execute("SELECT name FROM nodes WHERE node_type='METHOD'")}
    assert {"exportOrdersJob", "exportOrdersStep", "exportOrdersTasklet", "triggerExport",
            "notifyAsync", "bulkInsert", "removeOld"} <= method_names
