"""Query logic backing find_service / get_service_context / get_service_dependencies.

A "service" here is any distinct value of nodes.service - a Java
microservice, an Angular sub-project, the Angular ROOT_APP shell, or the
React app - the graph doesn't otherwise distinguish them structurally.
"""
import json

from ..db import fetch_all
from ..models.evidence import prune_empty
from .common import (
    DEPENDENCY_RELATIONS, is_entity_class, is_repository_class,
    inferred_table_name, list_distinct_services, service_classes,
    service_node_counts,
)

TECH_ANNOTATION_HINTS = {
    "SpringBootApplication": "Spring Boot",
    "RestController": "Spring Boot",
    "Entity": "JPA/Hibernate",
    "Document": "Spring Data MongoDB",
    "KafkaListener": "Kafka",
    "FeignClient": "Spring Cloud OpenFeign",
}
PROJECT_TYPE_TECH = {"angular": "Angular", "react": "React"}


def search_services(conn, keyword, limit):
    services = list_distinct_services(conn, keyword=keyword, limit=limit)
    out = []
    for svc in services:
        counts = service_node_counts(conn, svc["service"])
        out.append({
            "name": svc["service"],
            "type": "service",
            "project_type": svc["project_type"],
            "summary": _summarize_counts(svc["project_type"], counts),
        })
    return out


def _summarize_counts(project_type, counts):
    if project_type == "java":
        n_classes = counts.get("CLASS", 0) + counts.get("INTERFACE", 0) + counts.get("ENUM", 0)
        return f"Java service - {n_classes} classes, {counts.get('METHOD', 0)} methods, {counts.get('ENDPOINT', 0)} REST endpoints"
    if project_type == "angular":
        return (f"Angular module - {counts.get('NG_COMPONENT', 0)} components, "
                f"{counts.get('NG_SERVICE', 0)} services, {counts.get('NG_MODULE', 0)} modules")
    if project_type == "react":
        return f"React app - {counts.get('REACT_COMPONENT', 0)} components, {counts.get('REACT_HOOK', 0)} hooks"
    return f"{project_type or 'unknown'} project"


def service_exists(conn, service):
    counts = service_node_counts(conn, service)
    return bool(counts)


def _technology(conn, service, project_type, counts):
    tech = set()
    if project_type in PROJECT_TYPE_TECH:
        tech.add(PROJECT_TYPE_TECH[project_type])
    if project_type == "java":
        rows = fetch_all(conn, "SELECT metadata FROM nodes WHERE service = ? AND metadata IS NOT NULL", (service,))
        for row in rows:
            try:
                anns = json.loads(row["metadata"]).get("annotations", [])
            except (TypeError, ValueError):
                continue
            for ann in anns:
                if ann in TECH_ANNOTATION_HINTS:
                    tech.add(TECH_ANNOTATION_HINTS[ann])
    return sorted(tech)


def _events(conn, service):
    rows = fetch_all(
        conn,
        "SELECT DISTINCT name FROM nodes WHERE service = ? AND node_type IN "
        "('CLASS','INTERFACE') AND name LIKE '%Event' ORDER BY name LIMIT 20",
        (service,),
    )
    return [r["name"] for r in rows]


def _cross_service(conn, service, reverse=False):
    rows = _adjacent_services(conn, service, outgoing=not reverse)
    return sorted({r["other"] for r in rows})[:20]


def get_context(conn, service):
    counts = service_node_counts(conn, service)
    if not counts:
        return None

    project_type_row = fetch_all(conn, "SELECT project_type FROM nodes WHERE service = ? LIMIT 1", (service,))
    project_type = project_type_row[0]["project_type"] if project_type_row else None

    entities_count = 0
    tables_count = 0
    if project_type == "java":
        classes = service_classes(conn, service)
        entities_count = sum(1 for c in classes if is_entity_class(c))
        table_names = set()
        for c in classes:
            if is_repository_class(c):
                table_names.add(inferred_table_name(c["name"]))
        tables_count = len(table_names)

    return prune_empty({
        "service": service,
        "purpose": _summarize_counts(project_type, counts),
        "technology": _technology(conn, service, project_type, counts),
        "apis_count": counts.get("ENDPOINT", 0),
        "entities_count": entities_count,
        "tables_count": tables_count,
        "dependencies": _cross_service(conn, service, reverse=False),
        "dependents": _cross_service(conn, service, reverse=True),
        "events": _events(conn, service),
    })


def _adjacent_services(conn, service, outgoing):
    """One service's directly-connected *other* services - a compact
    service-level adjacency, not a per-class subgraph. All rows here are
    backed by a resolved edge (t.service can only be non-NULL if
    target_id resolved), so confidence is always 'verified'."""
    relations_ph = ",".join("?" for _ in DEPENDENCY_RELATIONS)
    if outgoing:
        sql = (f"SELECT DISTINCT t.service AS other, e.relation FROM edges e "
               f"JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id "
               f"WHERE s.service = ? AND t.service IS NOT NULL AND t.service != ? "
               f"AND e.relation IN ({relations_ph})")
    else:
        sql = (f"SELECT DISTINCT s.service AS other, e.relation FROM edges e "
               f"JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id "
               f"WHERE t.service = ? AND s.service IS NOT NULL AND s.service != ? "
               f"AND e.relation IN ({relations_ph})")
    return fetch_all(conn, sql, (service, service, *DEPENDENCY_RELATIONS))


def get_dependency_tree(conn, service, direction, depth, max_nodes):
    """Service-level BFS (never a per-class subgraph): each hop expands to
    directly-connected *services*, not individual classes/methods, so this
    stays cheap even for a service with tens of thousands of nodes."""
    counts = service_node_counts(conn, service)
    if not counts:
        return None

    visited = {service}
    frontier = [service]
    tree = []

    for d in range(1, depth + 1):
        if not frontier or len(tree) >= max_nodes:
            break
        next_frontier = []
        for svc in frontier:
            edge_sets = []
            if direction in ("outgoing", "both"):
                edge_sets.append((True, _adjacent_services(conn, svc, outgoing=True)))
            if direction in ("incoming", "both"):
                edge_sets.append((False, _adjacent_services(conn, svc, outgoing=False)))
            for is_outgoing, rows in edge_sets:
                for row in rows:
                    other = row["other"]
                    if len(tree) >= max_nodes:
                        break
                    tree.append({
                        "service": other,
                        "relation": row["relation"],
                        "direction": "outgoing" if is_outgoing else "incoming",
                        "depth": d,
                        "confidence": "verified",
                    })
                    if other not in visited:
                        visited.add(other)
                        next_frontier.append(other)
        frontier = next_frontier

    return tree
