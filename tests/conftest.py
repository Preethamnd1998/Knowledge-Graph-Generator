"""Builds a small synthetic knowledge graph via the *real* GraphDB API
(kg_indexer/db.py) — not hand-written SQL — so this fixture can never
silently drift from the schema the indexer actually produces. Covers:
a normal controller -> service -> repository chain, a cross-service
dependency, a naming-convention-detected domain event, an Angular
component, and a circular @Service dependency (to exercise cycle
protection in the traversal code).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from kg_indexer.db import GraphDB


def _build_fixture(path):
    db = GraphDB(path)

    # --- orders-service (java) ---
    db.add_node("SERVICE", "orders-service", qualified_name="java:orders-service",
                service="orders-service", project_type="java")

    controller = db.add_node(
        "CLASS", "OrderController", qualified_name="com.acme.orders.OrderController",
        service="orders-service", project_type="java",
        file_path="src/main/java/com/acme/orders/OrderController.java",
        start_line=10, end_line=60, signature="@RestController public class OrderController {",
        metadata={"annotations": ["RestController", "RequestMapping"]},
    )
    place_order = db.add_node(
        "METHOD", "placeOrder", qualified_name="com.acme.orders.OrderController#placeOrder(OrderRequestDTO)",
        service="orders-service", project_type="java", start_line=15,
        signature="ResponseEntity placeOrder(OrderRequestDTO dto)",
        metadata={"return_type": "ResponseEntity", "params": ["OrderRequestDTO"], "annotations": ["PostMapping"]},
    )
    cancel_order = db.add_node(
        "METHOD", "cancelOrder", qualified_name="com.acme.orders.OrderController#cancelOrder()",
        service="orders-service", project_type="java", start_line=30,
        signature="ResponseEntity cancelOrder(Long id)",
        metadata={"return_type": "ResponseEntity", "params": ["Long"], "annotations": ["PutMapping"]},
    )
    db.add_edge(controller, "CONTAINS", target_id=place_order, service="orders-service")
    db.add_edge(controller, "CONTAINS", target_id=cancel_order, service="orders-service")

    endpoint_place = db.add_node(
        "ENDPOINT", "POST /orders", qualified_name="ENDPOINT:orders-service:POST:/orders",
        service="orders-service", project_type="java",
        metadata={"http_method": "POST", "path": "/orders"},
    )
    endpoint_cancel = db.add_node(
        "ENDPOINT", "PUT /orders/{id}/cancel", qualified_name="ENDPOINT:orders-service:PUT:/orders/{id}/cancel",
        service="orders-service", project_type="java",
        metadata={"http_method": "PUT", "path": "/orders/{id}/cancel"},
    )
    db.add_edge(place_order, "EXPOSES", target_id=endpoint_place, service="orders-service")
    db.add_edge(cancel_order, "EXPOSES", target_id=endpoint_cancel, service="orders-service")

    order_service = db.add_node(
        "INTERFACE", "OrderService", qualified_name="com.acme.orders.OrderService",
        service="orders-service", project_type="java",
        metadata={"annotations": ["Service"]},
    )
    db.add_edge(controller, "DEPENDS_ON", target_id=order_service, service="orders-service",
                metadata={"via": "Autowired", "field_name": "orderService"})

    order_repo = db.add_node(
        "INTERFACE", "OrderRepository", qualified_name="com.acme.orders.OrderRepository",
        service="orders-service", project_type="java",
        metadata={"annotations": ["Repository"]},
    )
    db.add_edge(order_service, "DEPENDS_ON", target_id=order_repo, service="orders-service",
                metadata={"via": "Autowired", "field_name": "orderRepository"})
    find_by_id = db.add_node(
        "METHOD", "findById", qualified_name="com.acme.orders.OrderRepository#findById(Long)",
        service="orders-service", project_type="java",
        metadata={"return_type": "Optional", "params": ["Long"], "annotations": []},
    )
    save_method = db.add_node(
        "METHOD", "save", qualified_name="com.acme.orders.OrderRepository#save(Order)",
        service="orders-service", project_type="java",
        metadata={"return_type": "Order", "params": ["Order"], "annotations": []},
    )
    delete_method = db.add_node(
        "METHOD", "deleteById", qualified_name="com.acme.orders.OrderRepository#deleteById(Long)",
        service="orders-service", project_type="java",
        metadata={"return_type": "void", "params": ["Long"], "annotations": []},
    )
    for m in (find_by_id, save_method, delete_method):
        db.add_edge(order_repo, "CONTAINS", target_id=m, service="orders-service")

    order_created_event = db.add_node(
        "CLASS", "OrderCreatedEvent", qualified_name="com.acme.orders.OrderCreatedEvent",
        service="orders-service", project_type="java", metadata={"annotations": []},
    )

    # Circular @Service dependency — must not infinite-loop.
    service_a = db.add_node("CLASS", "ServiceA", qualified_name="com.acme.orders.ServiceA",
                             service="orders-service", project_type="java",
                             metadata={"annotations": ["Service"]})
    service_b = db.add_node("CLASS", "ServiceB", qualified_name="com.acme.orders.ServiceB",
                             service="orders-service", project_type="java",
                             metadata={"annotations": ["Service"]})
    db.add_edge(service_a, "DEPENDS_ON", target_id=service_b, service="orders-service")
    db.add_edge(service_b, "DEPENDS_ON", target_id=service_a, service="orders-service")

    # --- notification-service (java) — cross-service dependency target ---
    db.add_node("SERVICE", "notification-service", qualified_name="java:notification-service",
                service="notification-service", project_type="java")
    notification_service = db.add_node(
        "CLASS", "NotificationService", qualified_name="com.acme.notify.NotificationService",
        service="notification-service", project_type="java",
        metadata={"annotations": ["Service"]},
    )
    db.add_edge(order_service, "DEPENDS_ON", target_id=notification_service, service="orders-service",
                metadata={"via": "Autowired", "field_name": "notificationService"})

    # An edge whose target never resolves (simulates an external library import).
    db.add_edge(controller, "IMPORTS", target_name="HttpServletRequest", service="orders-service",
                metadata={"import_path": "javax.servlet.http.HttpServletRequest"})

    # --- orders-ui (angular) ---
    project_id = db.add_node("SERVICE", "lifetrenz-core", qualified_name="ng:lifetrenz-core",
                              project_type="angular")
    subproject_id = db.add_node("NG_SUBPROJECT", "orders-ui", qualified_name="ng:lifetrenz-core:orders-ui",
                                 service="orders-ui", project_type="angular")
    db.add_edge(project_id, "CONTAINS", target_id=subproject_id, service="lifetrenz-core")
    order_list_component = db.add_node(
        "NG_COMPONENT", "OrderListComponent", qualified_name="ng:lifetrenz-core:orders-ui:order-list.ts:OrderListComponent",
        service="orders-ui", project_type="angular", metadata={"decorator": "Component", "selector": "app-order-list"},
    )
    order_api_service = db.add_node(
        "NG_SERVICE", "OrderApiService", qualified_name="ng:lifetrenz-core:orders-ui:order-api.service.ts:OrderApiService",
        service="orders-ui", project_type="angular", metadata={"decorator": "Injectable"},
    )
    db.add_edge(order_list_component, "DEPENDS_ON", target_id=order_api_service, service="orders-ui",
                metadata={"param_name": "orderApi"})

    db.close()


@pytest.fixture(scope="session")
def fixture_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("kg_mcp_fixture") / "knowledge_graph.db")
    _build_fixture(path)
    return path


@pytest.fixture(autouse=True)
def _kg_db_path_env(fixture_db_path, monkeypatch):
    """Every test gets KG_DB_PATH pointed at the fixture automatically."""
    monkeypatch.setenv("KG_DB_PATH", fixture_db_path)
    monkeypatch.setenv("MCP_MAX_RESULTS", "10")
    monkeypatch.setenv("MCP_MAX_DEPTH", "3")
    yield
