"""Unit tests for the UI<->backend and backend<->backend API mapping added
to kg_indexer/parsers/{java_parser,spring_flow_parser,angular_parser}.py
and kg_indexer/db.py (resolve_service_call_edges / resolve_api_constant_edges).

Uses small synthetic services/UI projects (standard unit-test practice,
same convention as test_spring_flow_parser.py) — this is distinct from the
mandatory real-workspace acceptance check (see AGENTS.md / the indexer run
against ROOT_PATH), which needs the actual UI-BE source and isn't exercised
here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB
from kg_indexer.parsers.common import NameRegistry
from kg_indexer.parsers import angular_parser, java_parser, spring_flow_parser


def _write(base_dir, rel_path, content):
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")


BOOTSTRAP_YML = """
spring:
  application:
    name: {name}
"""

PATIENT_CONTROLLER_JAVA = """
package com.acme.core.patient;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/patient")
public class PatientController {

	@GetMapping("/list")
	public String list() {
		return "ok";
	}

	@GetMapping("/{id}")
	public String getById(@PathVariable Long id) {
		return "ok";
	}
}
"""

CORE_API_CONSTANT_JAVA = """
package com.acme.core.common;

public class CoreApiConstant {
	public static final String PATIENT_UPDATE = "patient/update";
}
"""

# @PostMapping references the constant symbolically, not a string literal —
# regression test for java_parser._collect_java_api_constants / _annotation_path.
PATIENT_UPDATE_CONTROLLER_JAVA = """
package com.acme.core.patient;

import com.acme.core.common.CoreApiConstant;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class PatientUpdateController {

	@PostMapping(value = CoreApiConstant.PATIENT_UPDATE)
	public String update() {
		return "ok";
	}
}
"""

MULTI_METHOD_CONTROLLER_JAVA = """
package com.acme.core.multi;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class MultiMethodController {

	@GetMapping("/multi")
	public String read() {
		return "ok";
	}

	@PostMapping("/multi")
	public String write() {
		return "ok";
	}
}
"""

DUPLICATE_CONTROLLER_JAVA = """
package com.acme.{pkg}.dup;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class DuplicateController {{

	@GetMapping("/duplicate/path")
	public String hit() {{
		return "ok";
	}}
}}
"""

FEIGN_PROXY_JAVA = """
package com.acme.billing.feign;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;

@FeignClient(name = "core")
public interface CoreServiceProxy {

	@GetMapping(value = "patient/list")
	String getPatients();
}
"""

FEIGN_PROXY_UNKNOWN_JAVA = """
package com.acme.billing.feign;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;

@FeignClient(name = "nonexistent-target")
public interface GhostServiceProxy {

	@GetMapping(value = "/whatever")
	String ghost();
}
"""

WEBCLIENT_SERVICE_JAVA = """
package com.acme.billing.client;

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

ORDER_PUBLISHER_JAVA = """
package com.acme.billing.kafka;

import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Service;

@Service
public class OrderPublisher {
	private final KafkaTemplate<String, String> kafkaTemplate;

	public OrderPublisher(KafkaTemplate<String, String> kafkaTemplate) {
		this.kafkaTemplate = kafkaTemplate;
	}

	public void publish(String payload) {
		kafkaTemplate.send("ORDER_CREATED", payload);
	}
}
"""

ORDER_CONSUMER_JAVA = """
package com.acme.invoicing.kafka;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Service;

@Service
public class OrderConsumer {

	@KafkaListener(topics = "ORDER_CREATED")
	public void onOrder(String payload) {
	}
}
"""

ORDERS_API_CONSTANT_TS = """
export class OrdersApiConstant {
  public static PATIENT_LIST = "core/patient/list";
  public static PATIENT_BY_ID = "core/patient/{id}";
  public static PATIENT_UPDATE = "core/patient/update";
  public static NOT_A_REAL_SERVICE = "totallyunknown/foo/bar";
  public static NO_ENDPOINT_HERE = "core/does/not/exist";
  public static MULTI_METHOD = "core/multi";
  public static DUP_BILLING = "billing/duplicate/path";
  public static DUP_INVOICING = "invoicing/duplicate/path";
}
"""

ORDERS_SERVICE_TS = """
import { OrdersApiConstant } from "./api-constant";

export class OrdersService {
  fetchPatients() {
    return this.http.get(OrdersApiConstant.PATIENT_LIST);
  }
}
"""


def _index_service(db, registry, service_name, service_path):
    stats = java_parser.parse_service(db, registry, service_name, str(service_path))
    flow_stats = spring_flow_parser.parse_service(db, registry, service_name, str(service_path))
    return stats, flow_stats


def _build_graph(tmp_path):
    db = GraphDB(str(tmp_path / "kg.db"))
    java_registry = NameRegistry()
    ng_registry = NameRegistry()

    # --- core-service ---
    core_dir = tmp_path / "core-service"
    _write(core_dir, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="core"))
    _write(core_dir, "src/main/java/com/acme/core/patient/PatientController.java", PATIENT_CONTROLLER_JAVA)
    _write(core_dir, "src/main/java/com/acme/core/common/CoreApiConstant.java", CORE_API_CONSTANT_JAVA)
    _write(core_dir, "src/main/java/com/acme/core/patient/PatientUpdateController.java", PATIENT_UPDATE_CONTROLLER_JAVA)
    _write(core_dir, "src/main/java/com/acme/core/multi/MultiMethodController.java", MULTI_METHOD_CONTROLLER_JAVA)

    # --- billing-service ---
    billing_dir = tmp_path / "billing-service"
    _write(billing_dir, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="billing"))
    _write(billing_dir, "src/main/java/com/acme/billing/dup/DuplicateController.java",
           DUPLICATE_CONTROLLER_JAVA.format(pkg="billing"))
    _write(billing_dir, "src/main/java/com/acme/billing/feign/CoreServiceProxy.java", FEIGN_PROXY_JAVA)
    _write(billing_dir, "src/main/java/com/acme/billing/feign/GhostServiceProxy.java", FEIGN_PROXY_UNKNOWN_JAVA)
    _write(billing_dir, "src/main/java/com/acme/billing/client/CoreWebClientService.java", WEBCLIENT_SERVICE_JAVA)
    _write(billing_dir, "src/main/java/com/acme/billing/kafka/OrderPublisher.java", ORDER_PUBLISHER_JAVA)

    # --- invoicing-service ---
    invoicing_dir = tmp_path / "invoicing-service"
    _write(invoicing_dir, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="invoicing"))
    _write(invoicing_dir, "src/main/java/com/acme/invoicing/dup/DuplicateController.java",
           DUPLICATE_CONTROLLER_JAVA.format(pkg="invoicing"))
    _write(invoicing_dir, "src/main/java/com/acme/invoicing/kafka/OrderConsumer.java", ORDER_CONSUMER_JAVA)

    gateway_prefix_map = {}
    for service_name, service_dir in (
        ("core-service", core_dir), ("billing-service", billing_dir), ("invoicing-service", invoicing_dir),
    ):
        stats, _flow_stats = _index_service(db, java_registry, service_name, service_dir)
        if stats.get("gateway_prefix"):
            gateway_prefix_map[stats["gateway_prefix"]] = service_name

    db.resolve_exact_import_edges("java")
    db.resolve_pending_edges(java_registry.as_dict(), project_type="java", exclude_relations=("IMPORTS",))

    # --- Angular UI ---
    ui_root = tmp_path / "lifetrenz-core"
    _write(ui_root, "projects/orders-ui/src/lib/constant/api-constant.ts", ORDERS_API_CONSTANT_TS)
    _write(ui_root, "projects/orders-ui/src/lib/service/orders.service.ts", ORDERS_SERVICE_TS)
    project_node_id = angular_parser.parse_project(db, "lifetrenz-core", str(ui_root))
    angular_parser.parse_subproject(
        db, ng_registry, "lifetrenz-core", project_node_id, "orders-ui",
        str(ui_root / "projects" / "orders-ui"),
    )
    db.resolve_pending_edges(ng_registry.as_dict(), project_type="angular")

    db.resolve_service_call_edges(gateway_prefix_map)
    api_result = db.resolve_api_constant_edges(gateway_prefix_map)

    db.commit()
    return db, gateway_prefix_map, api_result


def _calls_api_targets(db, constant_qname_suffix):
    row = db.conn.execute(
        "SELECT id FROM nodes WHERE node_type='API_CONSTANT' AND qualified_name LIKE ?",
        (f"%{constant_qname_suffix}",),
    ).fetchone()
    assert row is not None, f"API_CONSTANT {constant_qname_suffix} not found"
    edges = db.conn.execute(
        "SELECT t.service, t.qualified_name, e.metadata FROM edges e "
        "JOIN nodes t ON t.id = e.target_id WHERE e.source_id = ? AND e.relation = 'CALLS_API'",
        (row[0],),
    ).fetchall()
    return edges


def _constant_metadata(db, constant_qname_suffix):
    import json
    row = db.conn.execute(
        "SELECT metadata FROM nodes WHERE node_type='API_CONSTANT' AND qualified_name LIKE ?",
        (f"%{constant_qname_suffix}",),
    ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_exact_api_match(tmp_path):
    db, _gw, _api = _build_graph(tmp_path)
    edges = _calls_api_targets(db, "OrdersApiConstant.PATIENT_LIST")
    assert len(edges) == 1
    assert edges[0][0] == "core-service"
    assert edges[0][1] == "ENDPOINT:core-service:GET:/patient/list"


def test_controller_and_method_path_composition(tmp_path):
    """@RequestMapping("/patient") (class) + @GetMapping("/list") (method)
    must compose to /patient/list, and the symbolic-constant case
    (@PostMapping(value = CoreApiConstant.PATIENT_UPDATE)) must resolve to
    its literal "core/patient/update" value."""
    db, _gw, _api = _build_graph(tmp_path)
    edges = _calls_api_targets(db, "OrdersApiConstant.PATIENT_UPDATE")
    assert len(edges) == 1
    assert edges[0][1] == "ENDPOINT:core-service:POST:/patient/update"


def test_gateway_prefix_handling(tmp_path):
    """"core/..." strips the "core" gateway prefix (core-service's own
    spring.application.name, read from its bootstrap.yml) before matching
    against core-service's own ENDPOINT paths."""
    db, gateway_prefix_map, _api = _build_graph(tmp_path)
    assert gateway_prefix_map == {"core": "core-service", "billing": "billing-service",
                                   "invoicing": "invoicing-service"}


def test_path_variable_matches(tmp_path):
    edges = _calls_api_targets(*_build_graph(tmp_path)[:1], "OrdersApiConstant.PATIENT_BY_ID")
    assert len(edges) == 1
    assert edges[0][1] == "ENDPOINT:core-service:GET:/patient/{id}"


def test_unresolved_unknown_gateway_prefix(tmp_path):
    db, _gw, _api = _build_graph(tmp_path)
    meta = _constant_metadata(db, "OrdersApiConstant.NOT_A_REAL_SERVICE")
    assert meta["resolution"] == "unknown_gateway_prefix"
    edges = _calls_api_targets(db, "OrdersApiConstant.NOT_A_REAL_SERVICE")
    assert edges == []


def test_unresolved_no_matching_endpoint(tmp_path):
    db, _gw, _api = _build_graph(tmp_path)
    meta = _constant_metadata(db, "OrdersApiConstant.NO_ENDPOINT_HERE")
    assert meta["resolution"] == "no_matching_endpoint"
    assert meta["resolved_service"] == "core-service"
    assert _calls_api_targets(db, "OrdersApiConstant.NO_ENDPOINT_HERE") == []


def test_duplicate_path_disambiguated_by_gateway_prefix(tmp_path):
    """billing-service and invoicing-service both expose GET /duplicate/path
    — the UI constant's gateway prefix must route to the correct one, never
    both (a same literal path in two services is not cross-service
    ambiguity, the prefix already disambiguates it)."""
    db, _gw, _api = _build_graph(tmp_path)
    billing_edges = _calls_api_targets(db, "OrdersApiConstant.DUP_BILLING")
    invoicing_edges = _calls_api_targets(db, "OrdersApiConstant.DUP_INVOICING")
    assert [e[0] for e in billing_edges] == ["billing-service"]
    assert [e[0] for e in invoicing_edges] == ["invoicing-service"]


def test_ambiguous_multiple_http_methods_same_path(tmp_path):
    """Two endpoints (GET and POST) share the same path within the SAME
    resolved service — both are genuinely valid targets, so both CALLS_API
    edges are created and metadata.ambiguous is set, never guessed down to
    one."""
    db, _gw, _api = _build_graph(tmp_path)
    edges = _calls_api_targets(db, "OrdersApiConstant.MULTI_METHOD")
    assert len(edges) == 2
    assert {e[1] for e in edges} == {
        "ENDPOINT:core-service:GET:/multi", "ENDPOINT:core-service:POST:/multi",
    }
    import json
    assert all(json.loads(e[2])["ambiguous"] for e in edges)


def test_feign_call_resolved_and_unknown_target(tmp_path):
    db, _gw, _api = _build_graph(tmp_path)
    import json
    resolved = db.conn.execute(
        "SELECT s.name, t.qualified_name, e.metadata FROM edges e "
        "JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id "
        "WHERE e.relation = 'FEIGN_CALL' AND s.name = 'getPatients'"
    ).fetchone()
    assert resolved is not None
    assert resolved[1] == "ENDPOINT:core-service:GET:/patient/list"
    assert json.loads(resolved[2])["confidence"] == "resolved"

    unresolved = db.conn.execute(
        "SELECT s.name, e.target_id, e.metadata FROM edges e "
        "JOIN nodes s ON s.id = e.source_id WHERE e.relation = 'FEIGN_CALL' AND s.name = 'ghost'"
    ).fetchone()
    assert unresolved is not None
    assert unresolved[1] is None
    assert json.loads(unresolved[2])["resolution"] == "unknown_target_prefix"


def test_webclient_call_resolved(tmp_path):
    db, _gw, _api = _build_graph(tmp_path)
    import json
    row = db.conn.execute(
        "SELECT t.qualified_name, e.metadata FROM edges e "
        "JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id "
        "WHERE e.relation = 'WEBCLIENT_CALL' AND s.name = 'getPatients'"
    ).fetchone()
    assert row is not None
    assert row[0] == "ENDPOINT:core-service:GET:/patient/list"
    assert json.loads(row[1])["confidence"] == "resolved"


def test_kafka_cross_service_mapping_still_works(tmp_path):
    """Not new behavior (spring_flow_parser.py's KAFKA_TOPIC is
    service-agnostic by design — see its module docstring) but the explicit
    acceptance criterion in the task asks for this to be covered here too."""
    db, _gw, _api = _build_graph(tmp_path)
    topic = db.conn.execute("SELECT id FROM nodes WHERE node_type = 'KAFKA_TOPIC' AND name = 'ORDER_CREATED'").fetchone()
    assert topic is not None
    producer = db.conn.execute(
        "SELECT s.service FROM edges e JOIN nodes s ON s.id = e.source_id "
        "WHERE e.target_id = ? AND e.relation = 'PUBLISHES'", (topic[0],),
    ).fetchone()
    consumer = db.conn.execute(
        "SELECT s.service FROM edges e JOIN nodes s ON s.id = e.source_id "
        "WHERE e.target_id = ? AND e.relation = 'CONSUMES'", (topic[0],),
    ).fetchone()
    assert producer[0] == "billing-service"
    assert consumer[0] == "invoicing-service"


def test_uses_api_constant_edge_links_ui_file_to_constant(tmp_path):
    """The Angular service file that references OrdersApiConstant.PATIENT_LIST
    gets a USES_API_CONSTANT edge to that constant node — this is what lets
    'which UI screens call this backend endpoint' answer with a real file,
    not just the constant name."""
    db, _gw, _api = _build_graph(tmp_path)
    row = db.conn.execute(
        "SELECT id FROM nodes WHERE node_type='API_CONSTANT' AND qualified_name LIKE ?",
        ("%OrdersApiConstant.PATIENT_LIST",),
    ).fetchone()
    callers = db.conn.execute(
        "SELECT s.file_path FROM edges e JOIN nodes s ON s.id = e.source_id "
        "WHERE e.target_id = ? AND e.relation = 'USES_API_CONSTANT'", (row[0],),
    ).fetchall()
    assert any("orders.service.ts" in c[0] for c in callers)
