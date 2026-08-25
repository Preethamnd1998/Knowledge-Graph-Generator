"""Unit tests for kg_indexer/flow_tracer.py — the business flow tracer
built on top of the existing graph (see that module's docstring and
AGENTS.md "Business flow tracing"). Synthetic fixtures, same convention as
test_api_mapping.py / test_spring_flow_parser.py (standard unit-test
practice) — distinct from the mandatory real-workspace acceptance checks
run manually against D:\\GitLab-Workspace\\UI-BE per AGENTS.md.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer import flow_tracer as ft
from kg_indexer.db import GraphDB
from kg_indexer.parsers.common import NameRegistry
from kg_indexer.parsers import angular_parser, java_parser, spring_flow_parser


def _write(base_dir, rel_path, content):
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


BOOTSTRAP_YML = "spring:\n  application:\n    name: {name}\n"

CORE_CONTROLLER_JAVA = """
package com.acme.core.patient;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/patient")
public class PatientController {

	@GetMapping("/list")
	public String list() {
		return "ok";
	}
}
"""

BILLING_CONTROLLER_JAVA = """
package com.acme.billing.controller;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class BillingController {
	private final PriceBookUploadServiceImpl priceBookUploadService;

	public BillingController(PriceBookUploadServiceImpl priceBookUploadService) {
		this.priceBookUploadService = priceBookUploadService;
	}

	@PostMapping("/confirm/payment")
	public String confirmPayment() {
		return "ok";
	}
}
"""

# Mirrors the real-workspace acceptance example this task names explicitly:
# PriceBookUploadServiceImpl.promoteValidatedPriceBook() -> RUNS -> the batch
# job, via jobLauncher.run(this.<qualifierField>, ...).
PRICE_BOOK_SERVICE_JAVA = """
package com.acme.billing.service;

import org.springframework.batch.core.Job;
import org.springframework.batch.core.JobParametersBuilder;
import org.springframework.batch.core.launch.JobLauncher;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;

@Service
public class PriceBookUploadServiceImpl {

	@Autowired
	private JobLauncher jobLauncher;

	@Autowired
	@Qualifier("createPriceBookFromTempJob")
	private Job priceBookJob;

	public void promoteValidatedPriceBook() throws Exception {
		this.jobLauncher.run(this.priceBookJob, new JobParametersBuilder().toJobParameters());
	}
}
"""

PRICE_BOOK_BATCH_CONFIG_JAVA = """
package com.acme.billing.batch;

import org.springframework.batch.core.Job;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.job.builder.JobBuilder;
import org.springframework.batch.core.step.builder.StepBuilder;
import org.springframework.batch.core.step.tasklet.Tasklet;
import org.springframework.batch.repeat.RepeatStatus;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.kafka.core.KafkaTemplate;

@Configuration
public class PriceBookBatchConfig {

	@Autowired
	private JobRepository jobRepository;

	@Autowired
	private KafkaTemplate<String, String> kafkaTemplate;

	@Bean
	public Job createPriceBookFromTempJob() {
		return new JobBuilder("createPriceBookFromTempJob", this.jobRepository)
				.start(createPriceBookStep()).build();
	}

	@Bean
	public Step createPriceBookStep() {
		return new StepBuilder("createPriceBookStep", this.jobRepository)
				.tasklet(priceBookTasklet(), this.transactionManager).build();
	}

	@Bean
	public Tasklet priceBookTasklet() {
		return (contribution, chunkContext) -> {
			jdbcTemplate.update("INSERT INTO price_book (id, name) VALUES (?, ?)", 1, "x");
			kafkaTemplate.send("PRICE_BOOK_TOPIC", "promoted");
			return RepeatStatus.FINISHED;
		};
	}
}
"""

IPD_KAFKA_CONSUMER_JAVA = """
package com.acme.ipd.kafka;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Service;

@Service
public class PriceBookSyncConsumer {

	@KafkaListener(topics = "PRICE_BOOK_TOPIC")
	public void onPriceBookPromoted(String payload) {
	}
}
"""

IPD_FEIGN_PROXY_JAVA = """
package com.acme.ipd.feign;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;

@FeignClient(name = "core")
public interface CoreServiceProxy {

	@GetMapping(value = "patient/list")
	String getPatients();
}
"""

IPD_WEBCLIENT_SERVICE_JAVA = """
package com.acme.ipd.client;

import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClient;

@Service
public class CoreWebClientService {
	private final WebClient webClient;

	public CoreWebClientService(WebClient.Builder builder) {
		this.webClient = builder.baseUrl("http://core").build();
	}

	public String getPatients() {
		return webClient.get()
				.uri(uriBuilder -> uriBuilder.path("/patient/list").build())
				.retrieve().bodyToMono(String.class).block();
	}
}
"""

BILLING_API_CONSTANT_TS = """
export class BillingApiConstant {
  public static CONFIRM_PAYMENT = "billing/confirm/payment";
}
"""

BILLING_SERVICE_TS = """
import { Injectable } from "@angular/core";
import { BillingApiConstant } from "../constant/billing-api-constant";

@Injectable()
export class BillingService {
  confirmPayment() {
    return this.http.post(BillingApiConstant.CONFIRM_PAYMENT, {});
  }
}
"""


def _build_graph(tmp_path):
    db = GraphDB(str(tmp_path / "kg.db"))
    java_registry = NameRegistry()
    ng_registry = NameRegistry()

    core_dir = tmp_path / "core-service"
    _write(core_dir, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="core"))
    _write(core_dir, "src/main/java/com/acme/core/patient/PatientController.java", CORE_CONTROLLER_JAVA)

    billing_dir = tmp_path / "billing-service"
    _write(billing_dir, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="billing"))
    _write(billing_dir, "src/main/java/com/acme/billing/controller/BillingController.java", BILLING_CONTROLLER_JAVA)
    _write(billing_dir, "src/main/java/com/acme/billing/service/PriceBookUploadServiceImpl.java", PRICE_BOOK_SERVICE_JAVA)
    _write(billing_dir, "src/main/java/com/acme/billing/batch/PriceBookBatchConfig.java", PRICE_BOOK_BATCH_CONFIG_JAVA)

    ipd_dir = tmp_path / "ipd-service"
    _write(ipd_dir, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="ipd"))
    _write(ipd_dir, "src/main/java/com/acme/ipd/kafka/PriceBookSyncConsumer.java", IPD_KAFKA_CONSUMER_JAVA)
    _write(ipd_dir, "src/main/java/com/acme/ipd/feign/CoreServiceProxy.java", IPD_FEIGN_PROXY_JAVA)
    _write(ipd_dir, "src/main/java/com/acme/ipd/client/CoreWebClientService.java", IPD_WEBCLIENT_SERVICE_JAVA)

    gateway_prefix_map = {}
    for service_name, service_dir in (
        ("core-service", core_dir), ("billing-service", billing_dir), ("ipd-service", ipd_dir),
    ):
        stats = java_parser.parse_service(db, java_registry, service_name, str(service_dir))
        spring_flow_parser.parse_service(db, java_registry, service_name, str(service_dir))
        if stats.get("gateway_prefix"):
            gateway_prefix_map[stats["gateway_prefix"]] = service_name

    db.resolve_exact_import_edges("java")
    db.resolve_pending_edges(java_registry.as_dict(), project_type="java", exclude_relations=("IMPORTS",))

    ui_root = tmp_path / "lifetrenz-core"
    _write(ui_root, "projects/billing-ui/src/lib/constant/billing-api-constant.ts", BILLING_API_CONSTANT_TS)
    _write(ui_root, "projects/billing-ui/src/lib/service/billing.service.ts", BILLING_SERVICE_TS)
    project_node_id = angular_parser.parse_project(db, "lifetrenz-core", str(ui_root))
    angular_parser.parse_subproject(
        db, ng_registry, "lifetrenz-core", project_node_id, "billing-ui",
        str(ui_root / "projects" / "billing-ui"),
    )
    db.resolve_pending_edges(ng_registry.as_dict(), project_type="angular")

    db.resolve_service_call_edges(gateway_prefix_map)
    db.resolve_api_constant_edges(gateway_prefix_map)
    db.commit()

    db.conn.row_factory = __import__("sqlite3").Row
    return db.conn


def _confidences(paths):
    return {hop["confidence"] for path in paths for hop in path}


def _relations(paths):
    return {hop["relation"] for path in paths for hop in path}


def test_ui_constant_to_ipd_confirms_flow(tmp_path):
    """Billing API -> IPD-ish backend (acceptance test #1 shape): UI
    constant resolves to its endpoint with VERIFIED confidence."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "BillingApiConstant.CONFIRM_PAYMENT", direction="forward", max_depth=2)
    assert result["resolved"]
    root = result["results"][0]
    first_hops = [p[0] for p in root["forward_paths"]]
    assert any(h["relation"] == "CALLS_API" and h["confidence"] == ft.VERIFIED for h in first_hops)
    endpoint_hop = next(h for h in first_hops if h["relation"] == "CALLS_API")
    assert endpoint_hop["target"]["service"] == "billing-service"
    assert endpoint_hop["path_type"] == "DIRECT"


def test_endpoint_to_ui_caller_reverse(tmp_path):
    """REST endpoint -> UI callers (acceptance test #8 / #2 shape)."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "POST /confirm/payment", direction="reverse", max_depth=3)
    assert result["resolved"]
    root = result["results"][0]
    reached_names = {hop["target"]["name"] for path in root["reverse_paths"] for hop in path if hop["target"].get("resolved")}
    assert "BillingApiConstant.CONFIRM_PAYMENT" in reached_names
    # UI file should also surface via USES_API_CONSTANT further up the reverse chain
    assert any("billing.service.ts" in (n or "") for n in reached_names)


def test_price_book_batch_db_kafka_downstream(tmp_path):
    """Acceptance test #3 shape: PriceBookUploadServiceImpl -RUNS-> Job
    -HAS_STEP-> Step -USES_TASKLET-> Tasklet, and the tasklet's own body
    -WRITES-> price_book and -PUBLISHES-> a topic a downstream service
    -CONSUMES-> (cross-service, matching AGENTS.md's Kafka design)."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "PriceBookUploadServiceImpl", direction="forward", max_depth=6)
    assert result["resolved"]
    root = result["results"][0]
    relations_seen = _relations(root["forward_paths"])
    assert "RUNS" in relations_seen
    assert "HAS_STEP" in relations_seen
    assert "USES_TASKLET" in relations_seen

    # RUNS must carry real source evidence (file/method) per spec section 7.
    runs_hop = next(h for path in root["forward_paths"] for h in path if h["relation"] == "RUNS")
    assert runs_hop["confidence"] == ft.INFERRED  # jobLauncher.run() site is a spring_flow_parser "inferred" fact
    assert runs_hop["evidence"]["symbol"] == "promoteValidatedPriceBook"

    # The BATCH_TASKLET node itself (DEFINES_BEAN target) carries no direct
    # outgoing edges — WRITES/READS/PUBLISHES live on the underlying bean
    # METHOD node instead (same qname, reached from the @Configuration
    # class via CONTAINS, a separate branch from DEFINES_BEAN->BATCH_TASKLET).
    # Verify that branch surfaces the real DB/Kafka hops.
    config_result = ft.trace(conn, "PriceBookBatchConfig", direction="forward", max_depth=3)
    config_root = config_result["results"][0]
    config_relations = _relations(config_root["forward_paths"])
    assert "WRITES" in config_relations
    assert "PUBLISHES" in config_relations
    write_hop = next(h for path in config_root["forward_paths"] for h in path if h["relation"] == "WRITES")
    assert write_hop["target"]["name"] == "price_book"
    assert write_hop["confidence"] == ft.INFERRED  # SQL-literal table extraction is spring_flow_parser "inferred"


def test_kafka_producer_topic_consumer_cross_service(tmp_path):
    """Acceptance test #6 shape: producer -> topic -> consumer, crossing
    from billing-service to ipd-service."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "PRICE_BOOK_TOPIC", direction="reverse", max_depth=2)
    assert result["resolved"]
    root = result["results"][0]
    producers = [h for path in root["reverse_paths"] for h in path if h["relation"] == "PUBLISHES"]
    assert producers and producers[0]["target"]["service"] == "billing-service"

    forward = ft.trace(conn, "PRICE_BOOK_TOPIC", direction="forward", max_depth=2)
    fwd_root = forward["results"][0]
    consumers = [h for path in fwd_root["forward_paths"] for h in path if h["relation"] == "CONSUMES"]
    assert consumers
    consumer_hop = consumers[0]
    assert consumer_hop["confidence"] == ft.VERIFIED  # @KafkaListener is a verified fact
    assert consumer_hop["target"]["service"] == "ipd-service"


def test_db_table_reverse_to_writer(tmp_path):
    """Acceptance test #7 shape: DB table -> reverse callers."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "price_book", direction="reverse", max_depth=2)
    assert result["resolved"]
    root = result["results"][0]
    writers = [h for path in root["reverse_paths"] for h in path if h["relation"] == "WRITES"]
    assert writers
    assert writers[0]["source"]["service"] == "billing-service"


def test_feign_call_to_backend_endpoint(tmp_path):
    """Acceptance test #4 shape."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "CoreServiceProxy", direction="forward", max_depth=2)
    assert result["resolved"]
    root = result["results"][0]
    feign_hops = [h for path in root["forward_paths"] for h in path if h["relation"] == "FEIGN_CALL"]
    assert feign_hops
    assert feign_hops[0]["confidence"] == ft.VERIFIED
    assert feign_hops[0]["target"]["service"] == "core-service"


def test_webclient_call_to_backend_endpoint(tmp_path):
    """Acceptance test #5 shape."""
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "CoreWebClientService", direction="forward", max_depth=2)
    assert result["resolved"]
    root = result["results"][0]
    webclient_hops = [h for path in root["forward_paths"] for h in path if h["relation"] == "WEBCLIENT_CALL"]
    assert webclient_hops
    assert webclient_hops[0]["confidence"] == ft.VERIFIED
    assert webclient_hops[0]["target"]["service"] == "core-service"


def test_unresolvable_kafka_style_topic_stays_unresolved(tmp_path):
    """Section 6 requirement: an edge that exists but whose target never
    resolved must be reported UNRESOLVED, never upgraded to VERIFIED/INFERRED."""
    conn = _build_graph(tmp_path)
    node = conn.execute(
        "SELECT id FROM nodes WHERE node_type='CLASS' AND name='PriceBookUploadServiceImpl'"
    ).fetchone()
    conn.execute(
        "INSERT INTO edges (source_id, target_id, target_name, relation, service, metadata) "
        "VALUES (?, NULL, 'UNKNOWN_TOPIC_CONST', 'PUBLISHES', 'billing-service', "
        "'{\"confidence\": \"unknown\"}')",
        (node["id"],),
    )
    conn.commit()
    result = ft.trace(conn, "PriceBookUploadServiceImpl", direction="forward", max_depth=1)
    root = result["results"][0]
    unresolved_hops = [h for path in root["forward_paths"] for h in path
                        if h["relation"] == "PUBLISHES" and h["target"]["name"] == "UNKNOWN_TOPIC_CONST"]
    assert unresolved_hops
    assert unresolved_hops[0]["confidence"] == ft.UNRESOLVED
    assert unresolved_hops[0]["target"]["resolved"] is False


def test_cycle_detection_stops_recursion(tmp_path):
    """A -DEPENDS_ON-> B -DEPENDS_ON-> A must terminate, not loop forever,
    and the closing hop is marked so the cycle is visible rather than hidden."""
    db = GraphDB(str(tmp_path / "cyc.db"))
    a = db.add_node("CLASS", "ServiceA", qualified_name="com.acme.ServiceA", service="svc", project_type="java")
    b = db.add_node("CLASS", "ServiceB", qualified_name="com.acme.ServiceB", service="svc", project_type="java")
    db.add_edge(a, "DEPENDS_ON", target_id=b, service="svc")
    db.add_edge(b, "DEPENDS_ON", target_id=a, service="svc")
    db.commit()
    db.conn.row_factory = __import__("sqlite3").Row

    result = ft.trace(db.conn, "ServiceA", direction="forward", max_depth=10)
    assert result["resolved"]
    root = result["results"][0]
    assert any(h.get("cycle") for path in root["forward_paths"] for h in path)
    # Never runs away — depth capped, path count capped.
    assert all(len(p) <= 10 for p in root["forward_paths"])


def test_ambiguous_target_traces_all_candidates(tmp_path):
    """resolve_target must never silently pick one candidate when several
    real nodes share a name — an ambiguous target traces from every match,
    each clearly attributed, rather than guessing."""
    conn = _build_graph(tmp_path)
    conn.execute(
        "INSERT INTO nodes (node_type, name, qualified_name, service, project_type) "
        "VALUES ('CLASS', 'CoreServiceProxy', 'com.acme.other.CoreServiceProxy', 'ipd-service', 'java')"
    )
    conn.commit()
    candidates = ft.resolve_target(conn, "CoreServiceProxy")
    assert len(candidates) == 2
    result = ft.trace(conn, "CoreServiceProxy", direction="forward", max_depth=1)
    assert result["ambiguous"] is True
    assert len(result["results"]) == 2


def test_unresolved_target_reports_not_found(tmp_path):
    conn = _build_graph(tmp_path)
    result = ft.trace(conn, "ThisDoesNotExistAnywhere12345", direction="both")
    assert result["resolved"] is False
    assert "message" in result
