"""SQLite schema and manager for the unified code knowledge graph.

The graph is stored as two tables — nodes and edges — so the same schema
can represent Java classes/methods, Angular components/modules, and React
components/hooks without a different table per language. Richer per-type
detail (annotations, HTTP verb, selector, hook names, ...) lives in each
node/edge's `metadata` JSON column.
"""
import json
import os
import sqlite3

from .parsers.common import path_segments_match, path_specificity

SCHEMA = """
CREATE TABLE nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_type       TEXT NOT NULL,
    name            TEXT NOT NULL,
    qualified_name  TEXT UNIQUE,
    service         TEXT,
    project_type    TEXT,
    file_path       TEXT,
    start_line      INTEGER,
    end_line        INTEGER,
    signature       TEXT,
    metadata        TEXT
);

CREATE TABLE edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES nodes(id),
    target_id       INTEGER REFERENCES nodes(id),
    target_name     TEXT,
    relation        TEXT NOT NULL,
    service         TEXT,
    metadata        TEXT
);

CREATE INDEX idx_nodes_type ON nodes(node_type);
CREATE INDEX idx_nodes_service ON nodes(service);
CREATE INDEX idx_nodes_name ON nodes(name);
CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_relation ON edges(relation);
"""


class GraphDB:
    """Populates one SQLite file with the unified node/edge graph.

    A fresh run always rebuilds the schema from scratch (this is a batch
    reindex tool, not an incremental updater), so any previous .db file at
    `path` is replaced.
    """

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._qname_cache = {}  # qualified_name -> node id, this run only

    def add_node(self, node_type, name, qualified_name=None, service=None,
                 project_type=None, file_path=None, start_line=None,
                 end_line=None, signature=None, metadata=None):
        if qualified_name and qualified_name in self._qname_cache:
            return self._qname_cache[qualified_name]

        cur = self.conn.execute(
            "INSERT INTO nodes (node_type, name, qualified_name, service, "
            "project_type, file_path, start_line, end_line, signature, "
            "metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_type, name, qualified_name, service, project_type,
                file_path, start_line, end_line, signature,
                json.dumps(metadata) if metadata else None,
            ),
        )
        node_id = cur.lastrowid
        if qualified_name:
            self._qname_cache[qualified_name] = node_id
        return node_id

    def resolve(self, qualified_name):
        return self._qname_cache.get(qualified_name)

    def add_edge(self, source_id, relation, target_id=None, target_name=None,
                 service=None, metadata=None):
        self.conn.execute(
            "INSERT INTO edges (source_id, target_id, target_name, relation, "
            "service, metadata) VALUES (?, ?, ?, ?, ?, ?)",
            (
                source_id, target_id, target_name, relation, service,
                json.dumps(metadata) if metadata else None,
            ),
        )

    def resolve_exact_import_edges(self, project_type):
        """Resolve IMPORTS edges by exact fully-qualified name match against
        the import statement's declared path (stored in metadata as
        "import_path" by the Java parser). This only marks an import as
        internal when the *exact* dotted path matches an indexed class, so
        a library import that happens to share a simple name with a local
        class (e.g. org.springframework.data.jpa.domain.Query vs. some
        service's own "Query" DTO) is never confused for it. Should be run
        before resolve_pending_edges (which excludes IMPORTS for the same
        project_type once this has had a chance to resolve them precisely).
        """
        cur = self.conn.execute(
            "SELECT e.id, e.metadata FROM edges e JOIN nodes s ON s.id = e.source_id "
            "WHERE e.relation = 'IMPORTS' AND e.target_id IS NULL AND s.project_type = ?",
            (project_type,),
        )
        rows = cur.fetchall()
        resolved = 0
        for edge_id, metadata_json in rows:
            if not metadata_json:
                continue
            import_path = json.loads(metadata_json).get("import_path")
            if not import_path:
                continue
            target_id = self._qname_cache.get(import_path)
            if target_id is None:
                continue
            self.conn.execute("UPDATE edges SET target_id = ? WHERE id = ?", (target_id, edge_id))
            resolved += 1
        return resolved

    def resolve_pending_edges(self, name_registry, project_type=None, exclude_relations=()):
        """Second pass: fill in target_id for edges that were recorded with
        only a target_name (because the target node did not exist yet, or
        lives in a different file/service parsed earlier or later).

        `name_registry` maps a simple/short name -> qualified_name for the
        node types that are resolvable this way (classes, NG components,
        services, etc). Ambiguous names (multiple candidates) are flagged
        in the edge metadata rather than guessed at silently.

        `project_type` scopes the scan to edges whose *source* node belongs
        to that language (e.g. "java") — required in single-database mode,
        where edges from every language share one edges table and an
        Angular import named e.g. "Name" must never be resolved against an
        unrelated Java class also named "Name".

        `exclude_relations` skips relations already handled more precisely
        elsewhere (e.g. "IMPORTS" once resolve_exact_import_edges has run —
        simple-name matching is too loose for imports, see that method).
        """
        exclude_relations = tuple(exclude_relations)
        if project_type:
            if exclude_relations:
                placeholders = ",".join("?" for _ in exclude_relations)
                cur = self.conn.execute(
                    "SELECT e.id, e.target_name, e.metadata FROM edges e "
                    "JOIN nodes s ON s.id = e.source_id "
                    "WHERE e.target_id IS NULL AND e.target_name IS NOT NULL "
                    f"AND s.project_type = ? AND e.relation NOT IN ({placeholders})",
                    (project_type, *exclude_relations),
                )
            else:
                cur = self.conn.execute(
                    "SELECT e.id, e.target_name, e.metadata FROM edges e "
                    "JOIN nodes s ON s.id = e.source_id "
                    "WHERE e.target_id IS NULL AND e.target_name IS NOT NULL "
                    "AND s.project_type = ?",
                    (project_type,),
                )
        else:
            cur = self.conn.execute(
                "SELECT id, target_name, metadata FROM edges WHERE target_id IS "
                "NULL AND target_name IS NOT NULL"
            )
        rows = cur.fetchall()
        resolved = 0
        for edge_id, target_name, metadata_json in rows:
            candidates = name_registry.get(target_name)
            if not candidates:
                continue
            qname = candidates[0]
            target_id = self._qname_cache.get(qname)
            if target_id is None:
                continue
            metadata = json.loads(metadata_json) if metadata_json else {}
            if len(candidates) > 1:
                metadata["ambiguous_candidates"] = candidates
            self.conn.execute(
                "UPDATE edges SET target_id = ?, metadata = ? WHERE id = ?",
                (target_id, json.dumps(metadata) if metadata else None, edge_id),
            )
            resolved += 1
        return resolved

    def resolve_service_call_edges(self, gateway_prefix_map,
                                    relations=("FEIGN_CALL", "WEBCLIENT_CALL", "RESTTEMPLATE_CALL")):
        """Resolve backend->backend call edges (Feign/WebClient/RestTemplate)
        by EXACT match against a target ENDPOINT's qualified_name — same
        precision philosophy as resolve_exact_import_edges, never a fuzzy
        guess. `gateway_prefix_map` is {registered-service-name -> our
        service_name}, built by the orchestrator from every backend
        service's own spring.application.name (see java_parser
        ._extract_gateway_prefix) — never hand-mapped in this file.

        Requires SINGLE_DATABASE=true (the caller only invokes this in
        shared mode) since the target ENDPOINT commonly lives in a
        different service's portion of the same graph.
        """
        placeholders = ",".join("?" for _ in relations)
        cur = self.conn.execute(
            f"SELECT id, metadata FROM edges WHERE relation IN ({placeholders}) AND target_id IS NULL",
            relations,
        )
        rows = cur.fetchall()
        resolved = 0
        for edge_id, metadata_json in rows:
            metadata = json.loads(metadata_json) if metadata_json else {}
            if metadata.get("external_url"):
                continue
            target_prefix = metadata.get("target_prefix")
            http_method = metadata.get("http_method")
            path = metadata.get("path")
            if not target_prefix or not http_method or path is None:
                continue
            service_name = gateway_prefix_map.get(target_prefix.lower())
            if not service_name:
                metadata["resolution"] = "unknown_target_prefix"
                self.conn.execute("UPDATE edges SET metadata = ? WHERE id = ?",
                                   (json.dumps(metadata), edge_id))
                continue
            qname = f"ENDPOINT:{service_name}:{http_method}:{path}"
            target_id = self._qname_cache.get(qname)
            if target_id is None:
                metadata["resolved_service"] = service_name
                metadata["resolution"] = "no_matching_endpoint"
                self.conn.execute("UPDATE edges SET metadata = ? WHERE id = ?",
                                   (json.dumps(metadata), edge_id))
                continue
            metadata["confidence"] = "resolved"
            metadata["resolved_service"] = service_name
            self.conn.execute("UPDATE edges SET target_id = ?, metadata = ? WHERE id = ?",
                               (target_id, json.dumps(metadata), edge_id))
            resolved += 1
        return resolved

    def resolve_api_constant_edges(self, gateway_prefix_map):
        """Resolve UI API_CONSTANT nodes (see angular_parser.py /
        react_parser.py) to the backend ENDPOINT(s) they identify, creating
        a CALLS_API edge per match. Path-only matching (the constant itself
        carries no HTTP verb — see module docs), so a path shared by more
        than one HTTP method within the resolved service produces one
        CALLS_API edge per verb (precise, not ambiguous: each edge points at
        a genuinely different real endpoint). `metadata.resolution` on the
        API_CONSTANT node itself records *why* when nothing resolved, so an
        unresolved constant stays distinguishable from one never attempted.
        """
        cur = self.conn.execute(
            "SELECT id, service, metadata FROM nodes WHERE node_type = 'API_CONSTANT'"
        )
        constants = cur.fetchall()

        endpoint_cache = {}

        def _endpoints_for(service_name):
            if service_name not in endpoint_cache:
                rows = self.conn.execute(
                    "SELECT id, metadata FROM nodes WHERE node_type = 'ENDPOINT' AND service = ?",
                    (service_name,),
                ).fetchall()
                parsed = []
                for eid, emeta_json in rows:
                    emeta = json.loads(emeta_json) if emeta_json else {}
                    path = emeta.get("path") or ""
                    esegs = [s for s in path.strip("/").split("/") if s]
                    parsed.append((eid, esegs, emeta.get("http_method")))
                endpoint_cache[service_name] = parsed
            return endpoint_cache[service_name]

        resolved = ambiguous = unresolved = 0
        for node_id, ui_service, metadata_json in constants:
            metadata = json.loads(metadata_json) if metadata_json else {}
            segments = metadata.get("segments") or []
            if not segments:
                unresolved += 1
                continue
            prefix = segments[0].lower()
            service_name = gateway_prefix_map.get(prefix)
            if not service_name:
                metadata["resolution"] = "unknown_gateway_prefix"
                self.conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?",
                                   (json.dumps(metadata), node_id))
                unresolved += 1
                continue
            remaining = segments[1:]
            structural_matches = [
                (eid, esegs, http_method) for eid, esegs, http_method in _endpoints_for(service_name)
                if path_segments_match(remaining, esegs)
            ]
            if structural_matches:
                best = max(path_specificity(remaining, esegs) for _, esegs, _ in structural_matches)
                # A fully-wildcard endpoint (e.g. "/{id}/{type}") structurally
                # matches ANY same-length UI path with zero literal evidence
                # either is really the other — that's not a match, it's a
                # coincidence of segment count. Require at least one literal
                # segment in common whenever there's a non-empty path to
                # match against, so a spurious all-wildcard endpoint is never
                # the sole "resolution" for an unrelated UI constant.
                if best == 0 and remaining:
                    matches = []
                else:
                    matches = [(eid, http_method) for eid, esegs, http_method in structural_matches
                               if path_specificity(remaining, esegs) == best]
            else:
                matches = []
            if not matches:
                metadata["resolution"] = "no_matching_endpoint"
                metadata["resolved_service"] = service_name
                self.conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?",
                                   (json.dumps(metadata), node_id))
                unresolved += 1
                continue
            metadata["resolution"] = "resolved"
            metadata["resolved_service"] = service_name
            self.conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?",
                               (json.dumps(metadata), node_id))
            if len(matches) > 1:
                ambiguous += 1
            for eid, http_method in matches:
                self.add_edge(
                    node_id, "CALLS_API", target_id=eid, service=service_name,
                    metadata={"confidence": "resolved", "resolved_service": service_name,
                              "http_method": http_method, "ambiguous": len(matches) > 1},
                )
            resolved += 1
        return {"resolved": resolved, "ambiguous": ambiguous, "unresolved": unresolved}

    def summary(self):
        node_counts = dict(self.conn.execute(
            "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"
        ).fetchall())
        edge_counts = dict(self.conn.execute(
            "SELECT relation, COUNT(*) FROM edges GROUP BY relation"
        ).fetchall())
        unresolved = self.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE target_id IS NULL AND "
            "target_name IS NOT NULL"
        ).fetchone()[0]
        return {
            "nodes": node_counts,
            "edges": edge_counts,
            "unresolved_edges": unresolved,
            "total_nodes": sum(node_counts.values()),
            "total_edges": sum(edge_counts.values()),
        }

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
