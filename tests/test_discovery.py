"""Auto-discovery of BE / UI roots (kg_indexer/discovery.py) and the multi-project
orchestration built on it: split Angular repos (shell + sub-projects), Angular repos
with no sub-projects, the lifetrenz-core monolith, and several React apps — in one run.

The end-to-end tests run the real orchestrator over a tiny synthetic workspace; no
hand-written SQL builds the graph.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from kg_indexer import orchestrator
from kg_indexer.config import load_config
from kg_indexer.discovery import AngularTarget, Plan, classify_ui, discover, split_list
from kg_indexer.parsers.angular_parser import ROOT_APP_SENTINEL


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


COMPONENT = "@Component({{selector: 'x', template: ''}})\nexport class {name} {{}}\n"
REACT_APP = "export function {name}() {{\n  return <div />;\n}}\n"
CONTROLLER = (
    "package com.acme;\n"
    "@RestController\n"
    "@RequestMapping(\"/orders\")\n"
    "public class OrderController {\n"
    "    @GetMapping(\"/list\")\n"
    "    public String list() { return \"x\"; }\n"
    "}\n"
)


def _angular(root, name, projects=(), root_app=True, stale_json_projects=53):
    """An Angular repo. angular.json keeps a long project list that does not match
    the folders on disk — exactly what the split repos have."""
    base = os.path.join(root, name)
    _write(os.path.join(base, "angular.json"),
           json.dumps({"projects": {f"p{i}": {} for i in range(stale_json_projects)}}))
    if root_app:
        _write(os.path.join(base, "src", "app", "app.component.ts"), COMPONENT.format(name="ShellComponent"))
    for p in projects:
        _write(os.path.join(base, "projects", p, "src", "lib", f"{p}.component.ts"),
               COMPONENT.format(name=f"{p.title().replace('-', '')}Component"))
    return base


def _react(root, name, component):
    base = os.path.join(root, name)
    _write(os.path.join(base, "package.json"), json.dumps({"dependencies": {"react": "18"}, "devDependencies": {"vite": "5"}}))
    # byte-identical in every app, like a Vite template's entry file
    _write(os.path.join(base, "src", "main.tsx"), REACT_APP.format(name="Root"))
    _write(os.path.join(base, "src", "App.tsx"), REACT_APP.format(name=component))
    return base


@pytest.fixture
def workspace(tmp_path):
    root = str(tmp_path)
    # ---- BE
    _write(os.path.join(root, "BE", "orders-service", "pom.xml"), "<project/>")
    _write(os.path.join(root, "BE", "orders-service", "src/main/java/com/acme/OrderController.java"), CONTROLLER)
    _write(os.path.join(root, "BE", "ai-service", "requirements.txt"), "fastapi\n")
    _write(os.path.join(root, "BE", "relay", "package.json"), "{}")
    os.makedirs(os.path.join(root, "BE", "templates"))
    # ---- WAPP
    _angular(os.path.join(root, "WAPP"), "adt", projects=["adt", "patient-lib"])        # shell + sub-projects
    _angular(os.path.join(root, "WAPP"), "maintenance", projects=[])                    # root app only
    _angular(os.path.join(root, "WAPP"), "main", projects=["home", "lib"], root_app=False)  # no shell
    _angular(os.path.join(root, "WAPP"), "lifetrenz-core", projects=["masters", "settings"])  # monolith
    os.makedirs(os.path.join(root, "WAPP", "adt", "projects", "empty-submodule"))       # unpopulated submodule dir
    _react(os.path.join(root, "WAPP"), "notishell", "NotiApp")
    _react(os.path.join(root, "WAPP"), "gpt", "GptApp")
    _write(os.path.join(root, "WAPP", "docs-only", "README.md"), "x")
    return root


# ------------------------------------------------------------------ discovery
def test_classify_split_repo_uses_disk_not_angular_json(workspace):
    kind, subs = classify_ui(os.path.join(workspace, "WAPP", "adt"))
    assert kind == "angular"
    # root app first, then real folders; empty submodule dir dropped; the 53 stale
    # angular.json entries are ignored
    assert subs == [ROOT_APP_SENTINEL, "adt", "patient-lib"]


def test_classify_angular_without_subprojects_is_root_app_only(workspace):
    assert classify_ui(os.path.join(workspace, "WAPP", "maintenance")) == ("angular", [ROOT_APP_SENTINEL])


def test_classify_library_only_repo_has_no_root_app(workspace):
    assert classify_ui(os.path.join(workspace, "WAPP", "main")) == ("angular", ["home", "lib"])


def test_classify_react_and_unknown(workspace):
    assert classify_ui(os.path.join(workspace, "WAPP", "notishell")) == ("react", None)
    kind, reason = classify_ui(os.path.join(workspace, "WAPP", "docs-only"))
    assert kind is None and "no pom.xml / angular.json" in reason


def test_discover_builds_plan_and_reports_what_it_skipped(workspace):
    plan = discover(workspace, backend_roots=["BE"], ui_roots=["WAPP"])
    assert plan.backend == ["BE/orders-service"]
    assert [t.project for t in plan.angular] == ["WAPP/adt", "WAPP/lifetrenz-core", "WAPP/main", "WAPP/maintenance"]
    assert [os.path.basename(p) for p in plan.react] == ["gpt", "notishell"]
    skipped = dict(plan.skipped)
    assert "Python" in skipped["BE/ai-service"]
    assert "Node" in skipped["BE/relay"]
    assert skipped["BE/templates"] == "empty folder"
    assert "WAPP/docs-only" in skipped


def test_discover_exclude_explicit_dedupe_and_missing_root(workspace):
    explicit = Plan(backend=["BE/orders-service"], angular=[AngularTarget("WAPP/adt", ["adt"])])
    plan = discover(workspace, ["BE"], ["WAPP", "NOPE"], exclude=["Main", "gpt"], plan=explicit)
    assert plan.backend == ["BE/orders-service"]                       # not repeated
    assert [t.project for t in plan.angular][0] == "WAPP/adt"          # explicit entry wins, kept as given
    assert plan.angular[0].subprojects == ["adt"]
    assert "WAPP/main" not in [t.project for t in plan.angular]        # excluded (case-insensitive)
    assert [os.path.basename(p) for p in plan.react] == ["notishell"]
    assert ("NOPE", "UI_ROOT not found on disk") in plan.skipped


def test_split_list_accepts_commas_and_semicolons():
    assert split_list(" UI, WAPP;X ,, ") == ["UI", "WAPP", "X"]
    assert split_list("") == []


# ------------------------------------------------------------- orchestration
def _config(workspace, tmp_path, extra=""):
    cfg = tmp_path / "kg.env"
    cfg.write_text(
        f"ROOT_PATH={workspace}\nBACKEND_ROOT=BE\nUI_ROOT=WAPP\nINDEX_MODE=all\n"
        f"OUTPUT_DIR={tmp_path / 'out'}\nSINGLE_DATABASE=true\n{extra}", encoding="utf-8")
    return load_config(str(cfg))


def _services(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT service, COUNT(*) FROM nodes WHERE service IS NOT NULL AND service != '' GROUP BY service")}
    finally:
        conn.close()


def test_one_run_indexes_every_layout(workspace, tmp_path):
    orchestrator.run(_config(workspace, tmp_path))
    services = _services(str(tmp_path / "out" / "knowledge_graph.db"))
    assert "BE/orders-service" in services
    # split repo: shell + qualified sub-projects
    assert {"WAPP/adt-root-app", "adt/adt", "adt/patient-lib"} <= set(services)
    assert "adt/empty-submodule" not in services
    # angular without sub-projects: only the shell
    assert "WAPP/maintenance-root-app" in services and not any(s.startswith("maintenance/") for s in services)
    # no shell: only sub-projects
    assert "WAPP/main-root-app" not in services and {"main/home", "main/lib"} <= set(services)
    # monolith keeps its bare sub-project names (existing queries key on them)
    assert {"WAPP/lifetrenz-core-root-app", "masters", "settings"} <= set(services)
    # both react apps present although they share src/main.tsx
    assert services.get("notishell") and services.get("gpt")


def _react_components(db_path, service):
    conn = sqlite3.connect(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM nodes WHERE node_type = 'REACT_COMPONENT' AND service = ?", (service,))}
    finally:
        conn.close()


def test_react_app_added_later_is_fully_indexed(workspace, tmp_path):
    """Both apps ship a byte-identical src/main.tsx. When the second app joins an
    existing graph, its copy must not be mistaken for the first app's unchanged file.
    (Apps are processed alphabetically, so the one added later is `notishell`, after `gpt`.)"""
    db_path = str(tmp_path / "out" / "knowledge_graph.db")
    orchestrator.run(_config(workspace, tmp_path, "DISCOVER_EXCLUDE=notishell\n"))
    assert _react_components(db_path, "gpt") >= {"Root", "GptApp"}
    assert not _react_components(db_path, "notishell")

    orchestrator.run(_config(workspace, tmp_path))                 # notishell now discovered; incremental
    assert _react_components(db_path, "notishell") >= {"Root", "NotiApp"}


def test_incremental_rerun_is_stable_and_touches_only_the_changed_app(workspace, tmp_path):
    cfg = _config(workspace, tmp_path)
    db_path = str(tmp_path / "out" / "knowledge_graph.db")
    orchestrator.run(cfg)
    first = _services(db_path)
    orchestrator.run(cfg)                                          # unchanged tree
    assert _services(db_path) == first

    _write(os.path.join(workspace, "WAPP", "gpt", "src", "App.tsx"),
           REACT_APP.format(name="GptApp") + "export function Extra() {\n  return <span />;\n}\n")
    orchestrator.run(cfg)
    assert "Extra" in _react_components(db_path, "gpt")
    assert _react_components(db_path, "notishell") >= {"Root", "NotiApp"}


def test_explicit_lists_still_work_without_discovery(workspace, tmp_path):
    cfg = tmp_path / "kg.env"
    cfg.write_text(
        f"ROOT_PATH={workspace}\nBACKEND_SERVICES_START\nBE/orders-service\nBACKEND_SERVICES_END\n"
        f"ANGULAR_PROJECT=WAPP/adt\nANGULAR_SUBPROJECTS_START\nROOT_APP\nadt\nANGULAR_SUBPROJECTS_END\n"
        f"REACT_PROJECT_PATH={workspace}/WAPP/notishell\nINDEX_MODE=all\nOUTPUT_DIR={tmp_path / 'out'}\n", encoding="utf-8")
    orchestrator.run(load_config(str(cfg)))
    services = _services(str(tmp_path / "out" / "knowledge_graph.db"))
    assert {"BE/orders-service", "WAPP/adt-root-app", "adt/adt", "notishell"} <= set(services)
    assert "adt/patient-lib" not in services and "gpt" not in services
