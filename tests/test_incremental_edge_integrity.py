"""Incremental graph integrity (Phase 5): delete_nodes_for_file must not let
independently-timed reindexing of two related files silently drop or
dangle the edge between them.

Root cause (fixed in db.py's delete_nodes_for_file): incoming-edge cleanup
used to be an unconditional DELETE keyed only on the deleted node ids,
which also removed edges whose SOURCE lives in a completely different,
unchanged file — a relationship that file never gets a chance to
recreate, since only the file that actually changed gets re-parsed. Fixed
by reverting those edges to the pending state (target_id -> NULL) instead
of deleting them outright; target_name is never cleared on resolution
(see resolve_pending_edges), so the existing resolve_* passes — already
run at the end of every indexing operation — pick the edge back up
automatically once the target's replacement node exists.

Uses the real GraphDB API throughout (never hand-written SQL for the
graph itself) so these tests can't drift from what the indexer actually
does — same philosophy as conftest.py and test_angular_semantic_trace.py.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB
from kg_indexer.parsers import angular_parser
from kg_indexer.parsers.common import NameRegistry


def _edges(db_path, relation=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM edges" + (" WHERE relation = ?" if relation else "")
    rows = conn.execute(sql, (relation,) if relation else ()).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _node_id(db_path, qualified_name):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT id FROM nodes WHERE qualified_name = ?", (qualified_name,)).fetchone()
    conn.close()
    return row[0] if row else None


def _reparse_file(db_path, service, file_path, node_name, edges_out=(), incremental=True):
    """Simulates a parser's real incremental contract for ONE file: purge
    its old nodes (as the orchestrator does before re-parsing), insert a
    fresh node, add outgoing pending edges, then run the resolution pass
    (as the orchestrator does at the end of every run, regardless of what
    changed)."""
    db = GraphDB(db_path, incremental=incremental) if os.path.exists(db_path) else GraphDB(db_path)
    db.delete_nodes_for_file(file_path, service)
    node_id = db.add_node("CLASS", node_name, qualified_name=f"test:{node_name}",
                           service=service, project_type="test", file_path=file_path)
    for relation, target_name in edges_out:
        db.add_edge(node_id, relation, target_name=target_name, service=service)
    registry = {}
    # Build a fresh registry from every currently-known test node (mirrors
    # orchestrator.py's _seeded_registry + this run's own new nodes).
    for name, qname in db.conn.execute(
        "SELECT name, qualified_name FROM nodes WHERE project_type='test' AND qualified_name IS NOT NULL"
    ):
        registry.setdefault(name, [])
        if qname not in registry[name]:
            registry[name].append(qname)
    db.resolve_pending_edges(registry, project_type="test")
    db.close()
    return node_id


# --- Invariant A/B/F: source→target survives independent reindex of either side ---

def test_edge_survives_independent_target_reindex(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")
    b_id_1 = _node_id(db_path, "test:B")

    edges = _edges(db_path, "DEPENDS_ON")
    assert len(edges) == 1
    assert edges[0]["target_id"] == b_id_1  # Invariant A: no dangling edge

    # B reindexed independently — A is NOT touched in this step.
    _reparse_file(db_path, "svc", "B.txt", "B")
    b_id_2 = _node_id(db_path, "test:B")
    assert b_id_2 != b_id_1  # confirms node ids are NOT stable across reindex

    edges = _edges(db_path, "DEPENDS_ON")
    assert len(edges) == 1  # Invariant B: relationship not permanently removed
    assert edges[0]["target_id"] == b_id_2  # re-linked to the CURRENT node, not dangling


def test_edge_survives_independent_source_reindex(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")

    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")])
    a_id = _node_id(db_path, "test:A")
    b_id = _node_id(db_path, "test:B")

    edges = _edges(db_path, "DEPENDS_ON")
    assert len(edges) == 1
    assert edges[0]["source_id"] == a_id
    assert edges[0]["target_id"] == b_id


def test_reverse_direction_target_to_source(tmp_path):
    """B --USES--> A (edge direction opposite to the file-change direction
    tested above) — A is reindexed independently, B is not."""
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B", edges_out=[("USES", "A")])
    a_id_1 = _node_id(db_path, "test:A")

    _reparse_file(db_path, "svc", "A.txt", "A")  # A gets a new id; B untouched
    a_id_2 = _node_id(db_path, "test:A")
    assert a_id_2 != a_id_1

    edges = _edges(db_path, "USES")
    assert len(edges) == 1
    assert edges[0]["target_id"] == a_id_2


def test_multiple_edges_between_same_pair_all_relink(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A",
                   edges_out=[("CALLS", "B"), ("IMPORTS", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")

    _reparse_file(db_path, "svc", "B.txt", "B")  # B reindexed again; A untouched
    b_id = _node_id(db_path, "test:B")

    calls = _edges(db_path, "CALLS")
    imports = _edges(db_path, "IMPORTS")
    assert len(calls) == 1 and calls[0]["target_id"] == b_id
    assert len(imports) == 1 and imports[0]["target_id"] == b_id


# --- Invariant E: deletion correctness ---

def test_deleted_target_file_leaves_pending_not_dangling(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")

    db = GraphDB(db_path, incremental=True)
    db.delete_nodes_for_file("B.txt", "svc")  # B removed from disk, never reparsed
    db.close()

    assert _node_id(db_path, "test:B") is None
    edges = _edges(db_path, "DEPENDS_ON")
    assert len(edges) == 1
    assert edges[0]["target_id"] is None  # pending, not dangling, not deleted
    assert edges[0]["target_name"] == "B"  # preserved for a future re-appearance


def test_recreated_file_re_resolves_previously_pending_edge(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")
    db = GraphDB(db_path, incremental=True)
    db.delete_nodes_for_file("B.txt", "svc")
    db.close()

    _reparse_file(db_path, "svc", "B.txt", "B")  # B.txt recreated later
    b_id = _node_id(db_path, "test:B")
    edges = _edges(db_path, "DEPENDS_ON")
    assert edges[0]["target_id"] == b_id  # relationship restored, no duplicate edge
    assert len(edges) == 1


def test_deleting_source_file_removes_its_outgoing_edge(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")

    db = GraphDB(db_path, incremental=True)
    db.delete_nodes_for_file("A.txt", "svc")  # A (the source) removed
    db.close()

    assert _edges(db_path, "DEPENDS_ON") == []  # correctly gone: A no longer asserts anything


# --- Rename (delete-old-name + create-new-name is the actual rename contract) ---

def test_rename_target_file_relinks_via_new_qualified_name(tmp_path):
    """A rename is delete-old + create-new at a DIFFERENT rel_path. The old
    B.txt's pending edge (target_name='B') stays correctly pending forever
    if nothing named 'B' ever reappears — matching how a genuinely deleted
    symbol already behaves, never a dangling reference."""
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")

    db = GraphDB(db_path, incremental=True)
    db.delete_nodes_for_file("B.txt", "svc")  # old path gone
    db.close()
    _reparse_file(db_path, "svc", "B_renamed.txt", "B")  # same NAME, new PATH

    b_id = _node_id(db_path, "test:B")
    edges = _edges(db_path, "DEPENDS_ON")
    assert edges[0]["target_id"] == b_id  # re-resolves by name, path is irrelevant to resolution


# --- Invariant C: deterministic rebuild ---

def test_repeated_sequential_reindex_is_deterministic(tmp_path):
    db_path = str(tmp_path / "g.db")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")], incremental=False)
    _reparse_file(db_path, "svc", "B.txt", "B")

    # index -> reindex B -> reindex B again -> reindex A -> reindex A again
    _reparse_file(db_path, "svc", "B.txt", "B")
    _reparse_file(db_path, "svc", "B.txt", "B")
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")])
    _reparse_file(db_path, "svc", "A.txt", "A", edges_out=[("DEPENDS_ON", "B")])

    a_id = _node_id(db_path, "test:A")
    b_id = _node_id(db_path, "test:B")
    edges = _edges(db_path, "DEPENDS_ON")
    assert len(edges) == 1  # no duplicate accumulation across repeated passes
    assert edges[0]["source_id"] == a_id
    assert edges[0]["target_id"] == b_id
    assert len(_edges(db_path)) == 1  # nothing else leaked in


# --- Cross-stack: real Angular fixture, component reindexed independently of its template ---

_COMPONENT_TS = """
import { Component } from '@angular/core';
import { PatientService } from '../services/patient.service';

@Component({
    selector: 'app-patient-registration',
    templateUrl: './patient-registration.component.html',
})
export class PatientRegistrationComponent {
    constructor(public patientService: PatientService) {}
    savePatient() { this.patientService.savePatient(); }
}
"""
_TEMPLATE_HTML = '<button (click)="savePatient()">Save</button>\n'
_SERVICE_TS = """
import { Injectable } from '@angular/core';
@Injectable({ providedIn: 'root' })
export class PatientService { savePatient() {} }
"""


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _index_angular(db_path, root, only_files=None):
    db = GraphDB(db_path, incremental=True) if os.path.exists(db_path) else GraphDB(db_path)
    registry = NameRegistry()
    for simple, qname in db.iter_name_qualified_pairs("angular"):
        registry.register(simple, qname)
    project_id = angular_parser.parse_project(db, "proj", root)
    stats = angular_parser.parse_subproject(db, registry, "proj", project_id, "sub", root, only_files=only_files)
    db.resolve_pending_edges(registry.as_dict(), project_type="angular")
    db.close()
    return stats


def test_cross_stack_component_reindex_preserves_template_event_link(tmp_path):
    root = str(tmp_path / "ng")
    _write(root, "src/patient-registration/patient-registration.component.ts", _COMPONENT_TS)
    _write(root, "src/patient-registration/patient-registration.component.html", _TEMPLATE_HTML)
    _write(root, "src/services/patient.service.ts", _SERVICE_TS)
    db_path = str(tmp_path / "g.db")
    _index_angular(db_path, root)  # full index

    ts_path = os.path.join(root, "src/patient-registration/patient-registration.component.ts")
    # Reindex ONLY the component .ts (its node id changes) -- the .html
    # template is NOT in only_files, mirroring the real bug scenario.
    _index_angular(db_path, root, only_files={ts_path})

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    comp = conn.execute("SELECT id FROM nodes WHERE node_type='NG_COMPONENT'").fetchone()
    declared_in = conn.execute("SELECT * FROM edges WHERE relation='DECLARED_IN'").fetchall()
    conn.close()

    assert len(declared_in) == 1
    assert declared_in[0]["target_id"] == comp["id"]  # not dangling, not dropped


# --- Cross-stack: synthetic backend chain (Controller -> Service -> Repository -> Entity -> Table) ---
# Uses direct GraphDB node/edge construction (not java_parser) since this
# exercises the DB-layer invariant generically, independent of any one
# language's parser -- the whole point of this phase (§7 "generically, not
# only Angular").

def test_backend_chain_survives_independent_reindex_of_each_link(tmp_path):
    db_path = str(tmp_path / "g.db")

    def link(file_path, name, node_type, edge_relation=None, edge_target=None, incremental=False):
        db = GraphDB(db_path, incremental=incremental) if os.path.exists(db_path) else GraphDB(db_path)
        db.delete_nodes_for_file(file_path, "be")
        node_id = db.add_node(node_type, name, qualified_name=f"be:{name}",
                               service="be", project_type="test", file_path=file_path)
        if edge_relation:
            db.add_edge(node_id, edge_relation, target_name=edge_target, service="be")
        registry = {n: [q] for n, q in db.conn.execute(
            "SELECT name, qualified_name FROM nodes WHERE project_type='test' AND service='be'")}
        db.resolve_pending_edges(registry, project_type="test")
        db.close()

    link("Controller.java", "Controller", "CLASS", "DEPENDS_ON", "Service")
    link("Service.java", "Service", "CLASS", "DEPENDS_ON", "Repository", incremental=True)
    link("Repository.java", "Repository", "INTERFACE", "DEPENDS_ON", "Entity", incremental=True)
    link("Entity.java", "Entity", "CLASS", "MAPS_TO_TABLE", "patient_table", incremental=True)
    db = GraphDB(db_path, incremental=True)
    db.add_node("TABLE", "patient_table", qualified_name="be:patient_table", service="be", project_type="test")
    db.resolve_pending_edges(
        {n: [q] for n, q in db.conn.execute(
            "SELECT name, qualified_name FROM nodes WHERE project_type='test' AND service='be'")},
        project_type="test")
    db.close()

    def chain_intact():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        ok = True
        for rel, src, tgt in [
            ("DEPENDS_ON", "Controller", "Service"),
            ("DEPENDS_ON", "Service", "Repository"),
            ("DEPENDS_ON", "Repository", "Entity"),
            ("MAPS_TO_TABLE", "Entity", "patient_table"),
        ]:
            src_id = conn.execute("SELECT id FROM nodes WHERE qualified_name=?", (f"be:{src}",)).fetchone()
            tgt_id = conn.execute("SELECT id FROM nodes WHERE qualified_name=?", (f"be:{tgt}",)).fetchone()
            e = conn.execute(
                "SELECT * FROM edges WHERE relation=? AND source_id=?", (rel, src_id[0])
            ).fetchone()
            ok = ok and e is not None and e["target_id"] == tgt_id[0]
        conn.close()
        return ok

    assert chain_intact()

    # Independently reindex each middle link, one at a time, in isolation --
    # the two neighboring files are NOT re-touched for that step.
    link("Repository.java", "Repository", "INTERFACE", "DEPENDS_ON", "Entity", incremental=True)
    assert chain_intact()
    link("Service.java", "Service", "CLASS", "DEPENDS_ON", "Repository", incremental=True)
    assert chain_intact()
    link("Entity.java", "Entity", "CLASS", "MAPS_TO_TABLE", "patient_table", incremental=True)
    assert chain_intact()
