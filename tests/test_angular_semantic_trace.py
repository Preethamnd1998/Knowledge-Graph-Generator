"""Angular semantic tracing: template event bindings ((click)="handler()"
and friends) and route configuration (path/component/canActivate*/
canMatch/canLoad/resolve/children). Parses real synthetic .ts/.html files
on disk through the actual angular_parser + incremental machinery (not
hand-written SQL) so these tests can never silently drift from what the
indexer really produces — same philosophy as conftest.py's fixture.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer import incremental
from kg_indexer.db import GraphDB
from kg_indexer.parsers import angular_parser
from kg_indexer.parsers.common import NameRegistry, walk_source_files

_ANGULAR_EXTS = (".ts", ".html")
_ANGULAR_IGNORED = (".spec.ts", ".d.ts")


def _write(root, rel_path, content):
    path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _index(db_path, project_root, incremental_mode=False):
    db = GraphDB(db_path, incremental=True) if os.path.exists(db_path) else GraphDB(db_path)
    registry = NameRegistry()
    for simple, qname in db.iter_name_qualified_pairs("angular"):
        registry.register(simple, qname)

    project_id = angular_parser.parse_project(db, "proj", project_root)

    only_files = None
    removed = 0
    if incremental_mode:
        all_files = list(walk_source_files(project_root, _ANGULAR_EXTS, ignored_suffixes=_ANGULAR_IGNORED))
        changed, _unchanged = incremental.classify_files(db, "sub", project_root, all_files)
        removed = incremental.sweep_deleted(db, "sub", project_root, all_files)
        only_files = set(changed)

    stats = angular_parser.parse_subproject(db, registry, "proj", project_id, "sub",
                                             project_root, only_files=only_files)
    resolved = db.resolve_pending_edges(registry.as_dict(), project_type="angular")
    db.close()
    return {"stats": stats, "resolved": resolved, "removed": removed}


def _nodes(db_path, node_type):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM nodes WHERE node_type = ?", (node_type,)).fetchall()
    conn.close()
    return [dict(r, metadata=json.loads(r["metadata"]) if r["metadata"] else {}) for r in rows]


def _edges(db_path, relation):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT e.*, tn.name AS resolved_target_name, tn.node_type AS resolved_target_type "
        "FROM edges e LEFT JOIN nodes tn ON tn.id = e.target_id WHERE e.relation = ?",
        (relation,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_COMPONENT_TS = """
import {{ Component }} from '@angular/core';
import {{ PatientService }} from '../services/patient.service';

@Component({{
    selector: 'app-patient-registration',
    templateUrl: './patient-registration.component.html',
}})
export class PatientRegistrationComponent {{
    activeTab: string = 'all';

    constructor(public patientService: PatientService) {{}}

    {handler}(event: any) {{
        this.patientService.savePatient();
    }}

    onChange(event: any) {{}}
}}
"""

_TEMPLATE_HTML = """
<button (click)="{handler}()">Save</button>
<input (change)="onChange($event)">
<div (click)="activeTab = 'all'">Tab</div>
<button (click)="patientService.savePatient()">Save via service</button>
"""

_SERVICE_TS = """
import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class PatientService {
    savePatient() {}
}
"""

_GUARD_TS = """
import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class AuthGuardService {}
"""

_RESOLVER_TS = """
import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class PatientResolver {}
"""

_ROUTING_TS = """
import {{ NgModule }} from '@angular/core';
import {{ RouterModule, Routes }} from '@angular/router';
import {{ PatientRegistrationComponent }} from './patient-registration/patient-registration.component';
import {{ AuthGuardService }} from './guards/auth-guard.service';
import {{ PatientResolver }} from './resolvers/patient-resolver.service';

const routes: Routes = [
    {{
        path: '{path}',
        component: PatientRegistrationComponent,
        canActivate: [AuthGuardService],
        resolve: {{ patient: PatientResolver }},
    }},
    {{
        path: 'patient/:id',
        component: PatientRegistrationComponent,
    }},
    {{
        path: 'parent',
        children: [
            {{ path: 'child', component: PatientRegistrationComponent }},
        ],
    }},
];

@NgModule({{
    imports: [RouterModule.forRoot(routes)],
    exports: [RouterModule],
}})
export class AppRoutingModule {{}}
"""


def _write_fixture(root, handler="savePatient", route_path="patient-registration"):
    _write(root, "src/patient-registration/patient-registration.component.ts",
           _COMPONENT_TS.format(handler=handler))
    _write(root, "src/patient-registration/patient-registration.component.html",
           _TEMPLATE_HTML.format(handler=handler))
    _write(root, "src/services/patient.service.ts", _SERVICE_TS)
    _write(root, "src/guards/auth-guard.service.ts", _GUARD_TS)
    _write(root, "src/resolvers/patient-resolver.service.ts", _RESOLVER_TS)
    _write(root, "src/app-routing.module.ts", _ROUTING_TS.format(path=route_path))


# --- Template events --------------------------------------------------------

def test_self_call_event_creates_unresolved_handles_event_edge(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    result = _index(db_path, root)
    assert result["stats"]["errors"] == 0

    events = _nodes(db_path, "TEMPLATE_EVENT")
    by_expr = {e["metadata"]["handler_expression"]: e for e in events}

    save = by_expr["savePatient()"]
    assert save["metadata"]["event_name"] == "click"
    assert save["metadata"]["handler_kind"] == "self_call"
    assert save["metadata"]["method_name"] == "savePatient"

    handles = [e for e in _edges(db_path, "HANDLES_EVENT") if e["source_id"] == save["id"]]
    assert len(handles) == 1
    assert handles[0]["target_name"] == "savePatient"
    # Never guessed: KG has no per-method registry, so this stays unresolved
    # for Unified Trace / Code Cortex to verify against the real AST.
    assert handles[0]["target_id"] is None


def test_change_event_with_dollar_event_arg(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    events = _nodes(db_path, "TEMPLATE_EVENT")
    change = next(e for e in events if e["metadata"]["event_name"] == "change")
    assert change["metadata"]["method_name"] == "onChange"
    assert change["metadata"]["handler_expression"] == "onChange($event)"


def test_assignment_handler_creates_no_handles_event_edge(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    events = _nodes(db_path, "TEMPLATE_EVENT")
    assignment = next(e for e in events if e["metadata"]["handler_expression"] == "activeTab = 'all'")
    assert assignment["metadata"]["handler_kind"] == "unsupported"
    handles = [e for e in _edges(db_path, "HANDLES_EVENT") if e["source_id"] == assignment["id"]]
    assert handles == []  # never guess a fake relationship


def test_member_access_handler_resolves_to_injected_service_class(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    events = _nodes(db_path, "TEMPLATE_EVENT")
    member = next(e for e in events if e["metadata"]["handler_expression"] == "patientService.savePatient()")
    assert member["metadata"]["handler_kind"] == "member_call"
    assert member["metadata"]["member_type"] == "PatientService"

    handles = [e for e in _edges(db_path, "HANDLES_EVENT") if e["source_id"] == member["id"]]
    assert len(handles) == 1
    # This one DOES resolve — PatientService is a real registered NG_SERVICE.
    assert handles[0]["resolved_target_name"] == "PatientService"
    assert handles[0]["resolved_target_type"] == "NG_SERVICE"


def test_event_declared_in_owning_component(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    declared_in = _edges(db_path, "DECLARED_IN")
    assert len(declared_in) == 4  # one per template event
    assert all(e["resolved_target_name"] == "PatientRegistrationComponent" for e in declared_in)
    assert all(e["resolved_target_type"] == "NG_COMPONENT" for e in declared_in)


# --- Routes ------------------------------------------------------------------

def test_route_with_component_guard_and_resolver(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    routes = _nodes(db_path, "NG_ROUTE")
    main = next(r for r in routes if r["name"] == "patient-registration")
    assert main["metadata"]["component_name"] == "PatientRegistrationComponent"
    assert main["metadata"]["guards"] == {"canActivate": ["AuthGuardService"]}
    assert main["metadata"]["resolvers"] == {"patient": "PatientResolver"}

    routes_to = [e for e in _edges(db_path, "ROUTES_TO") if e["source_id"] == main["id"]]
    assert routes_to[0]["resolved_target_name"] == "PatientRegistrationComponent"

    guarded_by = [e for e in _edges(db_path, "GUARDED_BY") if e["source_id"] == main["id"]]
    assert guarded_by[0]["resolved_target_name"] == "AuthGuardService"
    assert json.loads(guarded_by[0]["metadata"])["guard_type"] == "canActivate"

    resolved_by = [e for e in _edges(db_path, "RESOLVED_BY") if e["source_id"] == main["id"]]
    assert resolved_by[0]["resolved_target_name"] == "PatientResolver"
    assert json.loads(resolved_by[0]["metadata"])["resolver_key"] == "patient"


def test_parameterized_route_path_preserved(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    routes = _nodes(db_path, "NG_ROUTE")
    param_route = next(r for r in routes if r["name"] == "patient/:id")
    assert param_route["metadata"]["path"] == "patient/:id"


def test_child_route_nesting(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    routes = _nodes(db_path, "NG_ROUTE")
    parent = next(r for r in routes if r["name"] == "parent")
    child = next(r for r in routes if r["name"] == "child")

    contains = [e for e in _edges(db_path, "CONTAINS_ROUTE") if e["source_id"] == parent["id"]]
    assert len(contains) == 1
    assert contains[0]["target_id"] == child["id"]


# --- Lazy routes (Phase 4) ----------------------------------------------------

_LAZY_ROUTING_TS_PAREN_ARG = """
import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

const routes: Routes = [
    {
        path: 'patients',
        loadChildren: () =>
            import('./patients/patients.module').then(
                (m) => m.PatientsModule,
            ),
    },
];

@NgModule({
    imports: [RouterModule.forRoot(routes)],
    exports: [RouterModule],
})
export class AppRoutingModule {}
"""

_LAZY_ROUTING_TS_BARE_ARG = """
import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

const routes: Routes = [
    {
        path: 'inpatient',
        loadChildren: () => import('@inpatient/lib/inpatient.module').then(m => m.InpatientModule),
    },
];

@NgModule({
    imports: [RouterModule.forRoot(routes)],
    exports: [RouterModule],
})
export class AppRoutingModule {}
"""


def test_lazy_route_captures_module_path_and_symbol_paren_arg(tmp_path):
    root = str(tmp_path / "ng")
    _write(root, "src/app-routing.module.ts", _LAZY_ROUTING_TS_PAREN_ARG)
    db_path = str(tmp_path / "graph.db")
    result = _index(db_path, root)
    assert result["stats"]["errors"] == 0

    routes = _nodes(db_path, "NG_ROUTE")
    route = next(r for r in routes if r["name"] == "patients")
    assert route["metadata"]["lazy"] is True
    assert route["metadata"]["lazy_module_path"] == "./patients/patients.module"
    assert route["metadata"]["lazy_module_symbol"] == "PatientsModule"
    assert route["metadata"]["component_name"] is None


def test_lazy_route_captures_module_path_and_symbol_bare_arg(tmp_path):
    root = str(tmp_path / "ng")
    _write(root, "src/app-routing.module.ts", _LAZY_ROUTING_TS_BARE_ARG)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    routes = _nodes(db_path, "NG_ROUTE")
    route = next(r for r in routes if r["name"] == "inpatient")
    assert route["metadata"]["lazy_module_path"] == "@inpatient/lib/inpatient.module"
    assert route["metadata"]["lazy_module_symbol"] == "InpatientModule"


def test_lazy_route_never_fabricates_routes_to_edge(tmp_path):
    root = str(tmp_path / "ng")
    _write(root, "src/app-routing.module.ts", _LAZY_ROUTING_TS_PAREN_ARG)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    routes = _nodes(db_path, "NG_ROUTE")
    route = next(r for r in routes if r["name"] == "patients")
    routes_to = [e for e in _edges(db_path, "ROUTES_TO") if e["source_id"] == route["id"]]
    assert routes_to == []  # no component on this route -> never fabricated


# --- Reverse tracing ---------------------------------------------------------

def test_reverse_component_to_route_and_guard_to_route(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)

    guard = _nodes(db_path, "NG_SERVICE")
    auth_guard_id = next(n["id"] for n in guard if n["name"] == "AuthGuardService")
    guarded_by = _edges(db_path, "GUARDED_BY")
    routes_guarding_it = [e for e in guarded_by if e["target_id"] == auth_guard_id]
    assert len(routes_guarding_it) == 1  # guard -> route reverse lookup works via target_id


# --- Incremental correctness --------------------------------------------------

def test_incremental_handler_rename_removes_old_adds_new(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root, handler="savePatient")
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)
    # First incremental pass just establishes fingerprints (matches the
    # existing java/react incremental behavior — see incremental.py docs).
    _index(db_path, root, incremental_mode=True)

    _write_fixture(root, handler="newHandler")
    _index(db_path, root, incremental_mode=True)

    events = _nodes(db_path, "TEMPLATE_EVENT")
    handler_names = {e["metadata"].get("method_name") for e in events if e["metadata"].get("handler_kind") == "self_call"}
    assert "newHandler" in handler_names
    assert "savePatient" not in handler_names


def test_incremental_route_path_rename(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root, route_path="patient-registration")
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)
    _index(db_path, root, incremental_mode=True)

    _write_fixture(root, route_path="patient-register-new")
    _index(db_path, root, incremental_mode=True)

    route_names = {r["name"] for r in _nodes(db_path, "NG_ROUTE")}
    assert "patient-register-new" in route_names
    assert "patient-registration" not in route_names


def test_incremental_template_deletion_sweeps_events(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    _write(root, "src/deleteme/deleteme.component.ts",
           "import { Component } from '@angular/core';\n"
           "@Component({ selector: 'app-deleteme', templateUrl: './deleteme.component.html' })\n"
           "export class DeleteMeComponent { doThing() {} }\n")
    _write(root, "src/deleteme/deleteme.component.html", '<button (click)="doThing()">Go</button>\n')
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)
    _index(db_path, root, incremental_mode=True)

    assert any(e["metadata"].get("method_name") == "doThing" for e in _nodes(db_path, "TEMPLATE_EVENT"))

    os.remove(os.path.join(root, "src/deleteme/deleteme.component.html"))
    os.remove(os.path.join(root, "src/deleteme/deleteme.component.ts"))
    result = _index(db_path, root, incremental_mode=True)
    assert result["removed"] == 2

    remaining = _nodes(db_path, "TEMPLATE_EVENT")
    assert not any(e["metadata"].get("method_name") == "doThing" for e in remaining)
    assert not any("deleteme" in (e.get("file_path") or "") for e in remaining)


def test_incremental_zero_change_is_a_noop(tmp_path):
    root = str(tmp_path / "ng")
    _write_fixture(root)
    db_path = str(tmp_path / "graph.db")
    _index(db_path, root)
    _index(db_path, root, incremental_mode=True)  # settle fingerprints

    before_events = len(_nodes(db_path, "TEMPLATE_EVENT"))
    before_routes = len(_nodes(db_path, "NG_ROUTE"))

    result = _index(db_path, root, incremental_mode=True)
    assert result["stats"]["errors"] == 0
    assert result["stats"].get("TEMPLATE_EVENT", 0) == 0  # nothing reparsed
    assert result["stats"].get("routes", 0) == 0
    assert result["removed"] == 0

    assert len(_nodes(db_path, "TEMPLATE_EVENT")) == before_events
    assert len(_nodes(db_path, "NG_ROUTE")) == before_routes
