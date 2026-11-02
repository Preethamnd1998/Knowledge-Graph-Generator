"""Gateway-prefix resolution: literal / env / placeholder-default / explicit override /
unresolved, conflicts, duplicates, and the end-to-end mapping from source config to the
URL resolution the saga tools use (SERVICE node metadata -> gateway_prefix_map ->
normalize_target -> ENDPOINT lookup). Asserts actual prefixes and resulting service
mappings, not just that the parser ran."""
import json
import os
import sqlite3

import pytest

from kg_indexer.db import GraphDB
from kg_indexer.gateway_prefix import (
    DEFAULT_OVERRIDES_FILE, OverrideError, SOURCE_DEFAULT, SOURCE_ENV, SOURCE_LITERAL, SOURCE_OVERRIDE,
    declared_names, find_duplicates, load_overrides, resolve_gateway_prefix, strict_mode,
)
from kg_indexer.parsers import java_parser
from kg_indexer.parsers.common import NameRegistry
from mcp_server.queries.saga_queries import find_entry_endpoints, gateway_prefix_map, normalize_target

CONTROLLER = """
package com.acme.{pkg};

import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/settings")
public class SettingsController {{
	@PutMapping("/{{id}}")
	public String update() {{
		return "ok";
	}}
}}
"""


def _service(tmp_path, folder, bootstrap=None, application=None, with_controller=True):
    d = tmp_path / folder
    res = d / "src" / "main" / "resources"
    res.mkdir(parents=True)
    if bootstrap is not None:
        (res / "bootstrap.yml").write_text(bootstrap)
    if application is not None:
        (res / "application.yml").write_text(application)
    if with_controller:
        pkg = folder.replace("-", "")
        j = d / "src" / "main" / "java" / "com" / "acme" / pkg
        j.mkdir(parents=True)
        (j / "SettingsController.java").write_text(CONTROLLER.format(pkg=pkg))
    return str(d)


def yml(name):
    return f"spring:\n  application:\n    name: {name}\n"


NO_NAME_YML = "server:\n  port: 8080\n"
OV = {"BE/core-service": {"prefix": "core", "evidence": "legacy index"}}


# ---- 1. literal ------------------------------------------------------------
def test_literal_name_wins_and_is_marked_literal(tmp_path):
    p = _service(tmp_path, "ipd-service", yml("ipd"), with_controller=False)
    r = resolve_gateway_prefix("BE/ipd-service", p, {}, {})
    assert (r.prefix, r.source, r.status, r.warnings) == ("ipd", SOURCE_LITERAL, "resolved", [])


def test_literal_in_application_yml_when_bootstrap_has_placeholder(tmp_path):
    p = _service(tmp_path, "x-service", yml("${SPRING_APPLICATION_NAME}"), yml("xsvc"), with_controller=False)
    assert resolve_gateway_prefix("BE/x-service", p, {}, {}).prefix == "xsvc"


# ---- 2. placeholder + override ---------------------------------------------
def test_placeholder_resolved_by_explicit_override(tmp_path):
    p = _service(tmp_path, "core-service", yml("${SPRING_APPLICATION_NAME}"), with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, OV, {})
    assert (r.prefix, r.source) == ("core", SOURCE_OVERRIDE)
    assert r.warnings == []  # an override being used is informational, not a problem
    assert any("unresolvable name" in n and "legacy index" in n for n in r.notes)


def test_override_matches_by_folder_name_too(tmp_path):
    p = _service(tmp_path, "core-service", yml("${SPRING_APPLICATION_NAME}"), with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, {"core-service": {"prefix": "core"}}, {})
    assert r.prefix == "core"


# ---- 3. placeholder + environment ------------------------------------------
def test_placeholder_resolved_from_environment(tmp_path):
    p = _service(tmp_path, "core-service", yml("${SPRING_APPLICATION_NAME}"), with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, {}, {"SPRING_APPLICATION_NAME": "Core"})
    assert (r.prefix, r.source) == ("core", SOURCE_ENV)


def test_placeholder_default_value(tmp_path):
    p = _service(tmp_path, "core-service", yml("${SPRING_APPLICATION_NAME:core}"), with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, {}, {})
    assert (r.prefix, r.source) == ("core", SOURCE_DEFAULT)


def test_environment_value_disagreeing_with_override_is_a_conflict_env_wins(tmp_path):
    p = _service(tmp_path, "core-service", yml("${SPRING_APPLICATION_NAME}"), with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, OV, {"SPRING_APPLICATION_NAME": "core2"})
    assert (r.prefix, r.source) == ("core2", SOURCE_ENV)
    assert any("CONFLICT" in w for w in r.warnings)


# ---- 4. missing name + override --------------------------------------------
def test_missing_name_with_override(tmp_path):
    p = _service(tmp_path, "core-service", NO_NAME_YML, with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, OV, {})
    assert (r.prefix, r.source) == ("core", SOURCE_OVERRIDE)
    assert any("no spring.application.name" in n for n in r.notes) and r.warnings == []


def test_no_resources_dir_with_override(tmp_path):
    (tmp_path / "core-service").mkdir()
    assert resolve_gateway_prefix("BE/core-service", str(tmp_path / "core-service"), OV, {}).prefix == "core"


# ---- 5. missing name + no override -----------------------------------------
def test_missing_name_without_override_is_unresolved_never_guessed(tmp_path):
    p = _service(tmp_path, "billing-service", NO_NAME_YML, with_controller=False)
    r = resolve_gateway_prefix("BE/billing-service", p, {}, {})
    assert (r.prefix, r.source, r.status) == (None, None, "unresolved")
    assert "UNRESOLVED" in r.warnings[-1] and "gateway-prefix-overrides.json" in r.warnings[-1]  # not 'billing'


def test_unresolved_placeholder_without_override(tmp_path):
    p = _service(tmp_path, "ums-service", yml("${SPRING_APPLICATION_NAME}"), with_controller=False)
    r = resolve_gateway_prefix("BE/ums-service", p, {}, {})
    assert r.status == "unresolved" and "unresolved placeholder" in r.warnings[-1]


# ---- 6. empty name ---------------------------------------------------------
def test_empty_name_unresolved_then_override(tmp_path):
    p = _service(tmp_path, "core-service", "spring:\n  application:\n    name:\n", with_controller=False)
    assert declared_names(p) == [("bootstrap.yml", "")]
    assert resolve_gateway_prefix("BE/core-service", p, {}, {}).status == "unresolved"
    r = resolve_gateway_prefix("BE/core-service", p, OV, {})
    assert (r.prefix, r.source) == ("core", SOURCE_OVERRIDE)
    assert any("empty spring.application.name" in n for n in r.notes)


# ---- 7. conflicting override vs literal ------------------------------------
def test_literal_beats_conflicting_override_with_warning(tmp_path):
    p = _service(tmp_path, "core-service", yml("coresvc"), with_controller=False)
    r = resolve_gateway_prefix("BE/core-service", p, OV, {})
    assert (r.prefix, r.source) == ("coresvc", SOURCE_LITERAL)
    assert any("CONFLICT" in w and "'core'" in w for w in r.warnings)


def test_literal_equal_to_override_is_not_a_conflict(tmp_path):
    p = _service(tmp_path, "core-service", yml("core"), with_controller=False)
    assert resolve_gateway_prefix("BE/core-service", p, OV, {}).warnings == []


# ---- 8. duplicates ---------------------------------------------------------
def test_duplicate_prefixes_detected():
    assert find_duplicates({"BE/a": "core", "BE/b": "core", "BE/c": "ipd", "BE/d": None}) == {"core": ["BE/a", "BE/b"]}
    assert find_duplicates({"BE/a": "core", "BE/c": "ipd"}) == {}


def test_override_file_validation(tmp_path):
    f = tmp_path / "o.json"
    f.write_text(json.dumps({"overrides": {"BE/a": "core", "BE/b": "core"}}))
    with pytest.raises(OverrideError, match="share override prefix"):
        load_overrides(str(f), {})
    f.write_text(json.dumps({"overrides": {"BE/a": {"prefix": "Core Svc"}}}))
    with pytest.raises(OverrideError, match="invalid prefix"):
        load_overrides(str(f), {})
    f.write_text("{not json")
    with pytest.raises(OverrideError, match="invalid JSON"):
        load_overrides(str(f), {})
    with pytest.raises(OverrideError, match="not found"):
        load_overrides(str(tmp_path / "missing.json"), {})


def test_env_var_selects_alternate_overrides_file(tmp_path):
    f = tmp_path / "o.json"
    f.write_text(json.dumps({"overrides": {"BE/x": {"prefix": "xx", "evidence": "t"}}}))
    assert load_overrides(None, {"KG_GATEWAY_PREFIX_OVERRIDES": str(f)}) == {"BE/x": {"prefix": "xx", "evidence": "t"}}


def test_strict_mode_flag():
    assert strict_mode({"KG_GATEWAY_STRICT": "1"}) and not strict_mode({})


# ---- shipped overrides file ------------------------------------------------
def test_shipped_overrides_cover_exactly_the_three_required_services():
    ov = load_overrides(DEFAULT_OVERRIDES_FILE, {})
    assert {k: v["prefix"] for k, v in ov.items()} == {
        "BE/core-service": "core", "BE/ipd-clinical-service": "ipd-clinical", "BE/user-management-service": "ums",
    }
    assert all(v["evidence"] for v in ov.values())


# ---- 9-12. end-to-end: source config -> SERVICE metadata -> saga URL resolution
def _graph(tmp_path, specs, overrides):
    db = GraphDB(str(tmp_path / "kg.db"))
    reg = NameRegistry()
    stats = {}
    for folder, bootstrap in specs:
        p = _service(tmp_path, folder, bootstrap)
        name = f"BE/{folder}"
        stats[name] = java_parser.parse_service(db, reg, name, p, gateway_overrides=overrides, gateway_env={})
    db.commit()
    db.conn.row_factory = sqlite3.Row
    return db, stats


SPECS = [
    ("core-service", yml("${SPRING_APPLICATION_NAME}")),
    ("ipd-clinical-service", yml("${SPRING_APPLICATION_NAME}")),
    ("user-management-service", yml("${SPRING_APPLICATION_NAME}")),
    ("ipd-service", yml("ipd")),
    ("clinical-common-service", yml("ccs")),
]


@pytest.fixture
def graph(tmp_path):
    return _graph(tmp_path, SPECS, load_overrides(DEFAULT_OVERRIDES_FILE, {}))


@pytest.mark.parametrize("prefix,service", [
    ("core", "BE/core-service"),
    ("ipd-clinical", "BE/ipd-clinical-service"),
    ("ums", "BE/user-management-service"),
])
def test_required_gateway_routes_resolve_end_to_end(graph, prefix, service):
    db, stats = graph
    assert stats[service]["gateway_prefix"] == prefix and stats[service]["gateway_prefix_source"] == SOURCE_OVERRIDE
    meta = json.loads(db.conn.execute("SELECT metadata FROM nodes WHERE node_type='SERVICE' AND name=?", (service,)).fetchone()[0])
    assert meta == {"gateway_prefix": prefix, "gateway_prefix_source": "override"}
    assert gateway_prefix_map(db.conn)[prefix] == service
    svc, method, path = normalize_target(db.conn, f"https://canary-dev.example.com/api-gateway/{prefix}/settings/6a4b")
    assert (svc, path) == (service, "/settings/6a4b")
    found = find_entry_endpoints(db.conn, svc, "PUT", path)
    assert [(e["service"], e["path"]) for e in found] == [(service, "/settings/{id}")]


@pytest.mark.parametrize("prefix,service", [("ipd", "BE/ipd-service"), ("ccs", "BE/clinical-common-service")])
def test_literal_services_unchanged_by_the_override_mechanism(graph, prefix, service):
    db, stats = graph
    assert (stats[service]["gateway_prefix"], stats[service]["gateway_prefix_source"]) == (prefix, SOURCE_LITERAL)
    assert gateway_prefix_map(db.conn)[prefix] == service
    assert normalize_target(db.conn, f"/api-gateway/{prefix}/settings/1")[0] == service


def test_without_overrides_the_three_services_do_not_resolve(tmp_path):
    """Regression guard for the original failure: placeholder names + no override => no mapping, flagged."""
    db, stats = _graph(tmp_path, SPECS, {})
    assert set(gateway_prefix_map(db.conn)) == {"ipd", "ccs"}
    assert stats["BE/core-service"]["gateway_prefix"] is None
    assert any("UNRESOLVED" in w for w in stats["BE/core-service"]["gateway_prefix_warnings"])
    assert normalize_target(db.conn, "/api-gateway/core/settings/1")[0] is None
    unresolved = db.conn.execute("SELECT metadata FROM nodes WHERE node_type='SERVICE' AND name='BE/core-service'").fetchone()[0]
    assert json.loads(unresolved) == {"gateway_prefix_status": "unresolved"}
