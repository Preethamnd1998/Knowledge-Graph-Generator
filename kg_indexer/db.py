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

CREATE TABLE file_fingerprints (
    file_path       TEXT NOT NULL,
    service         TEXT NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    PRIMARY KEY (file_path, service)
);
"""


class GraphDB:
    """Populates one SQLite file with the unified node/edge graph.

    By default a fresh run rebuilds the schema from scratch (any previous
    .db file at `path` is replaced) — this is the "first run = full index"
    path. Pass incremental=True to open an existing .db in place instead:
    callers are then responsible for deleting stale per-file node/edge data
    themselves (see delete_nodes_for_file) before re-inserting, and for
    only re-parsing files that actually changed (see kg_indexer.incremental).
    """

    def __init__(self, path, incremental=False):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        is_fresh = not (incremental and os.path.exists(path))
        if is_fresh and os.path.exists(path):
            os.remove(path)
        self.conn = sqlite3.connect(path)
        if is_fresh:
            self.conn.executescript(SCHEMA)
        else:
            # Existing DB from a prior run — schema already present. Kept as
            # a no-op executescript(CREATE TABLE IF NOT EXISTS ...) so a .db
            # written before file_fingerprints existed still gets it added.
            self.conn.executescript(SCHEMA.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
                                     .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS "))
        self.is_fresh = is_fresh
        self._qname_cache = {}  # qualified_name -> node id, this run only
        if not is_fresh:
            # Seed from existing nodes so cross-file reference resolution
            # (resolve_pending_edges etc.) still sees symbols defined in
            # unchanged files that this run never re-parses.
            for qn, node_id in self.conn.execute(
                "SELECT qualified_name, id FROM nodes WHERE qualified_name IS NOT NULL"
            ):
                self._qname_cache[qn] = node_id

    def iter_name_qualified_pairs(self, project_type):
        """(simple_name, qualified_name) for every existing node of this
        project_type — used to seed a NameRegistry on an incremental run so
        cross-reference resolution (extends/implements/@Autowired/...) still
        sees symbols defined in unchanged files this run never re-parses."""
        return self.conn.execute(
            "SELECT name, qualified_name FROM nodes WHERE project_type = ? AND qualified_name IS NOT NULL",
            (project_type,),
        ).fetchall()

    def get_file_fingerprint(self, file_path, service):
        row = self.conn.execute(
            "SELECT mtime_ns, size, sha256 FROM file_fingerprints WHERE file_path = ? AND service = ?",
            (file_path, service),
        ).fetchone()
        if not row:
            return None
        return {"mtime_ns": row[0], "size": row[1], "sha256": row[2]}

    def set_file_fingerprint(self, file_path, service, mtime_ns, size, sha256):
        self.conn.execute(
            "INSERT INTO file_fingerprints (file_path, service, mtime_ns, size, sha256) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(file_path, service) DO UPDATE SET mtime_ns=excluded.mtime_ns, "
            "size=excluded.size, sha256=excluded.sha256",
            (file_path, service, mtime_ns, size, sha256),
        )

    def delete_file_fingerprint(self, file_path, service):
        self.conn.execute(
            "DELETE FROM file_fingerprints WHERE file_path = ? AND service = ?",
            (file_path, service),
        )

    def list_tracked_files(self, service):
        return [r[0] for r in self.conn.execute(
            "SELECT file_path FROM file_fingerprints WHERE service = ?", (service,)
        )]

    def delete_nodes_for_file(self, file_path, service):
        """Remove a file's nodes before re-parsing it, or because it was
        deleted from disk. Also drops the node from the in-run qname cache
        so a changed file's stale qualified_name doesn't shadow its freshly
        re-inserted replacement.

        Outgoing edges (this file's nodes as source) are hard-deleted —
        correct, since this same file is about to be re-parsed (or is gone
        for good) and will recreate whatever's still true.

        Incoming edges (this file's nodes as target) are reverted to
        PENDING (target_id -> NULL) rather than deleted. target_name is
        never cleared by resolve_pending_edges (see that method), so this
        is not a lossy operation — it drops the edge back into the exact
        state a never-yet-resolved edge is already in, and the resolve_*
        passes that already run at the end of every indexing operation
        (regardless of what changed) will re-link it to this file's
        freshly-inserted replacement node. Deleting these outright — the
        previous behavior — permanently lost any relationship whose SOURCE
        lives in a different, unchanged file that this run never re-parses
        to regenerate the edge (see angular_parser.py's TEMPLATE_EVENT
        DECLARED_IN edges for the real incident this was found from — but
        the bug applied to every edge type/language, not just Angular)."""
        rows = self.conn.execute(
            "SELECT id, qualified_name FROM nodes WHERE file_path = ? AND service = ?",
            (file_path, service),
        ).fetchall()
        ids = [r[0] for r in rows]
        for _id, qn in rows:
            if qn in self._qname_cache:
                del self._qname_cache[qn]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM edges WHERE source_id IN ({placeholders})", ids)
        self.conn.execute(f"UPDATE edges SET target_id = NULL WHERE target_id IN ({placeholders})", ids)
        self.conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", ids)
        return len(ids)

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

        # Base-constant values for derived `BASE + '/suffix'` nodes (same value across the
        # monolith copies, so the first indexed definition is as good as any).
        base_segments = {}
        for _id, _svc, _meta_json in constants:
            _meta = json.loads(_meta_json) if _meta_json else {}
            if _meta.get("segments") and not _meta.get("derived_from"):
                cname = f"{_meta.get('class_name')}.{_meta.get('field_name')}"
                base_segments.setdefault(cname, _meta["segments"])

        resolved = ambiguous = unresolved = 0
        for node_id, ui_service, metadata_json in constants:
            metadata = json.loads(metadata_json) if metadata_json else {}
            segments = metadata.get("segments") or []
            if metadata.get("derived_from"):
                base = base_segments.get(metadata["derived_from"])
                if not base:
                    metadata["resolution"] = "base_constant_not_found"
                    self.conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?",
                                       (json.dumps(metadata), node_id))
                    unresolved += 1
                    continue
                segments = list(base) + list(metadata.get("suffix_segments") or [])
                metadata["segments"] = segments
                metadata["normalized_path"] = "/".join(segments)
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
                    tied = [(eid, esegs, http_method) for eid, esegs, http_method in structural_matches
                            if path_specificity(remaining, esegs) == best]
                    # Tie-break: a UI `{var}` segment (`${id}`) belongs to an endpoint `{var}`
                    # before it belongs to a literal that merely happens to fit.
                    def _var_pairs(esegs):
                        return sum(1 for a, b in zip(remaining, esegs)
                                   if a.startswith("{") and a.endswith("}") and b.startswith("{") and b.endswith("}"))
                    best_vars = max(_var_pairs(esegs) for _, esegs, _ in tied)
                    matches = [(eid, http_method) for eid, esegs, http_method in tied
                               if _var_pairs(esegs) == best_vars]
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

    def prune_dangling_constant_usages(self):
        """The UI usage pattern now matches any `SomeConstant(s).FIELD`, but only
        URL-shaped constants become nodes. A usage that never resolved to one
        (e.g. `AppConstants.MAX_ROWS`) is noise: drop it. Unresolved usages of
        *ApiConstant classes are kept as before — those are meaningful (a real
        API constant whose definition isn't indexed)."""
        cur = self.conn.execute(
            "DELETE FROM edges WHERE relation = 'USES_API_CONSTANT' AND target_id IS NULL "
            "AND lower(COALESCE(target_name, '')) NOT LIKE '%apiconstant%'")
        self.conn.commit()
        return cur.rowcount

    def prune_orphan_edges(self):
        """Re-indexing a service deletes and recreates its nodes; edges FROM another project that
        pointed at a deleted node (a UI CALLS_API -> a backend ENDPOINT) are left with no target id
        and no target name. Nothing can ever resolve them, and the re-resolution pass has already
        added a fresh, correct edge, so they only double the counts. Drop them."""
        cur = self.conn.execute("DELETE FROM edges WHERE target_id IS NULL AND target_name IS NULL")
        self.conn.commit()
        return cur.rowcount

    def dedupe_edges(self):
        """Delete exact-duplicate edges (same relation, endpoints, target name,
        service AND metadata), keeping the oldest. Repeated incremental runs
        re-emit some edges (containment, resolved API-constant links, ...);
        without this the graph grows ~10% per few runs and every count/degree
        based on edges drifts. Edges that differ in metadata are never merged."""
        cur = self.conn.execute(
            "DELETE FROM edges WHERE id NOT IN (SELECT MIN(id) FROM edges GROUP BY relation, source_id, "
            "COALESCE(target_id, -1), COALESCE(target_name, ''), COALESCE(service, ''), COALESCE(metadata, ''))")
        self.conn.commit()
        return cur.rowcount

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
