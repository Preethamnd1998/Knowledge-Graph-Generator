"""UI endpoint constants kept in classes NOT named *ApiConstant
(e.g. SystemSettingsConstant.AUTO_FOLLOW_UP_RULES_POST_GET = "core/auto-follow-up-rules")."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kg_indexer.db import GraphDB
from kg_indexer.parsers import angular_parser
from kg_indexer.parsers.common import NameRegistry, looks_like_api_path
from tests.test_api_mapping import BOOTSTRAP_YML, _index_service, _write

RULES_CONTROLLER = """
package com.acme.core.settings;

import org.springframework.web.bind.annotation.*;

@RestController
public class RulesController {
    @GetMapping("/auto-follow-up-rules")
    public Object getRules() { return null; }
}
"""

SETTINGS_CONSTANT_TS = """
export class SystemSettingsConstant {
  public static AUTO_FOLLOW_UP_RULES_POST_GET = "core/auto-follow-up-rules";
  public static PAGE_SIZE = "10";
  public static LABEL = "hello world";
  public static ROUTE_ONLY = "settings";
}
"""

STRICT_API_CONSTANT_TS = """
export class LegacyApiConstant {
  public static NOT_A_PATH = "x";
}
"""

RULES_SERVICE_TS = """
import { SystemSettingsConstant } from '../constant/system-settings-constant';
import { AppConstants } from '../constant/app-constants';
export class RulesService {
  load() {
    return this.http.get(SystemSettingsConstant.AUTO_FOLLOW_UP_RULES_POST_GET);
  }
  rows() { return AppConstants.MAX_ROWS; }
  other() { return MissingApiConstant.GONE; }
}
"""


def _build(tmp_path):
    db = GraphDB(str(tmp_path / "kg.db"))
    java_registry, ng_registry = NameRegistry(), NameRegistry()
    core = tmp_path / "core-service"
    _write(core, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="core"))
    _write(core, "src/main/java/com/acme/core/settings/RulesController.java", RULES_CONTROLLER)
    stats, _ = _index_service(db, java_registry, "core-service", core)
    gateway = {stats["gateway_prefix"]: "core-service"}
    db.resolve_pending_edges(java_registry.as_dict(), project_type="java", exclude_relations=("IMPORTS",))

    ui = tmp_path / "lifetrenz-core"
    _write(ui, "projects/settings/src/lib/constant/system-settings-constant.ts", SETTINGS_CONSTANT_TS)
    _write(ui, "projects/settings/src/lib/constant/legacy.ts", STRICT_API_CONSTANT_TS)
    _write(ui, "projects/settings/src/lib/service/rules.service.ts", RULES_SERVICE_TS)
    pid = angular_parser.parse_project(db, "lifetrenz-core", str(ui))
    angular_parser.parse_subproject(db, ng_registry, "lifetrenz-core", pid, "settings", str(ui / "projects" / "settings"))
    db.resolve_pending_edges(ng_registry.as_dict(), project_type="angular")
    db.resolve_api_constant_edges(gateway)
    db.commit()
    return db


def test_url_shaped_fields_in_a_plain_constants_class_become_linked_api_constants(tmp_path):
    db = _build(tmp_path)
    names = {r[0] for r in db.conn.execute("SELECT name FROM nodes WHERE node_type='API_CONSTANT'")}

    assert "SystemSettingsConstant.AUTO_FOLLOW_UP_RULES_POST_GET" in names
    assert not {"SystemSettingsConstant.PAGE_SIZE", "SystemSettingsConstant.LABEL", "SystemSettingsConstant.ROUTE_ONLY"} & names
    assert "LegacyApiConstant.NOT_A_PATH" in names     # *ApiConstant classes keep the old, unfiltered behaviour

    linked = db.conn.execute(
        """SELECT t.name FROM edges e JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id
           WHERE e.relation = 'CALLS_API' AND s.name = 'SystemSettingsConstant.AUTO_FOLLOW_UP_RULES_POST_GET'""").fetchall()
    assert [r[0] for r in linked] == ["GET /auto-follow-up-rules"]

    used = db.conn.execute(
        """SELECT f.file_path FROM edges e JOIN nodes f ON f.id = e.source_id JOIN nodes t ON t.id = e.target_id
           WHERE e.relation = 'USES_API_CONSTANT' AND t.name = 'SystemSettingsConstant.AUTO_FOLLOW_UP_RULES_POST_GET'""").fetchall()
    assert [r[0] for r in used] == ["src/lib/service/rules.service.ts"]


def test_dangling_usages_pruned_only_for_non_api_constant_classes(tmp_path):
    db = _build(tmp_path)
    assert db.prune_dangling_constant_usages() >= 1
    left = {r[0] for r in db.conn.execute(
        "SELECT target_name FROM edges WHERE relation = 'USES_API_CONSTANT' AND target_id IS NULL")}
    assert "AppConstants.MAX_ROWS" not in left          # noise: not a URL constant
    assert "MissingApiConstant.GONE" in left            # meaningful: a real API-constant reference, definition not indexed


def test_looks_like_api_path():
    for ok in ("core/auto-follow-up-rules", "ipd/followup-rule/assignment/", "core/manage/x/{id}", "ipd/a.b/c"):
        assert looks_like_api_path(ok), ok
    for bad in ("10", "hello world", "settings", "", "https://x.y/z", "Core/Upper", "a/ b"):
        assert not looks_like_api_path(bad), bad


def test_gateway_prefixes_already_in_the_graph_seed_a_ui_only_run(tmp_path):
    from kg_indexer.orchestrator import _seed_gateway_prefix_map
    db = GraphDB(str(tmp_path / "s.db"))
    db.add_node("SERVICE", "BE/core-service", qualified_name="java:BE/core-service", service="BE/core-service",
                project_type="java", metadata={"gateway_prefix": "Core"})
    db.add_node("SERVICE", "UI/lifetrenz-core", qualified_name="ng:UI/lifetrenz-core", service="UI/lifetrenz-core",
                project_type="angular")
    db.commit()
    prefixes = {"ipd": "BE/ipd-service"}                  # this run's own discovery must win / be kept
    _seed_gateway_prefix_map(db, prefixes)
    assert prefixes == {"ipd": "BE/ipd-service", "core": "BE/core-service"}   # lowercased, UI service ignored


CHAT_CONTROLLER = """
package com.acme.supp.chat;

import org.springframework.web.bind.annotation.*;

@RestController
public class ChatController {
    @GetMapping("/chat/message/unread-count")
    public Object unread() { return null; }
    @GetMapping("/chat/message/{messageId}")
    public Object byId() { return null; }
    @GetMapping("/chat/message/{messageId}/read-by")
    public Object readBy() { return null; }
}
"""

CHAT_CONSTANT_TS = """
export class ChatApiConstant {
  public static GET_CHAT_MESSAGES_BASE = 'supplementary/chat/message';
}
"""

CHAT_SERVICE_TS = """
import { ChatApiConstant } from './chat-api-constant';
export class ChatApiService {
  unread() { return this.api.get(`${ChatApiConstant.GET_CHAT_MESSAGES_BASE}/unread-count`); }
  readBy(id) { return this.api.get(`${ChatApiConstant.GET_CHAT_MESSAGES_BASE}/${id}/read-by?x=1`); }
  legacy() { return this.api.get(ChatApiConstant.GET_CHAT_MESSAGES_BASE + '/unread-count'); }
  bare(id) { return this.api.get(`${ChatApiConstant.GET_CHAT_MESSAGES_BASE}/${id}`); }
  none() { return `${ChatApiConstant.GET_CHAT_MESSAGES_BASE}/`; }
}
"""


def _build_chat(tmp_path):
    db = GraphDB(str(tmp_path / "chat.db"))
    java_registry, ng_registry = NameRegistry(), NameRegistry()
    supp = tmp_path / "supplementary-service"
    _write(supp, "src/main/resources/bootstrap.yml", BOOTSTRAP_YML.format(name="supplementary"))
    _write(supp, "src/main/java/com/acme/supp/chat/ChatController.java", CHAT_CONTROLLER)
    stats, _ = _index_service(db, java_registry, "supplementary-service", supp)
    gateway = {stats["gateway_prefix"]: "supplementary-service"}
    db.resolve_pending_edges(java_registry.as_dict(), project_type="java", exclude_relations=("IMPORTS",))

    ui = tmp_path / "lifetrenz-core"
    _write(ui, "projects/chat/src/lib/chat-api-constant.ts", CHAT_CONSTANT_TS)
    _write(ui, "projects/chat/src/lib/chat-api.service.ts", CHAT_SERVICE_TS)
    pid = angular_parser.parse_project(db, "lifetrenz-core", str(ui))
    angular_parser.parse_subproject(db, ng_registry, "lifetrenz-core", pid, "chat", str(ui / "projects" / "chat"))
    db.resolve_pending_edges(ng_registry.as_dict(), project_type="angular")
    db.resolve_api_constant_edges(gateway)
    db.commit()
    return db


def test_constant_plus_literal_suffix_resolves_to_the_literal_endpoint(tmp_path):
    db = _build_chat(tmp_path)
    rows = db.conn.execute(
        """SELECT s.name, t.name FROM edges e JOIN nodes s ON s.id = e.source_id JOIN nodes t ON t.id = e.target_id
           WHERE e.relation = 'CALLS_API' AND s.name LIKE '%+ /%' ORDER BY s.name""").fetchall()
    got = {(a, b) for a, b in rows}

    # template and `+ '/x'` forms both land on the literal endpoint, NOT the {messageId} wildcard
    assert ("ChatApiConstant.GET_CHAT_MESSAGES_BASE + /unread-count", "GET /chat/message/unread-count") in got
    assert ("ChatApiConstant.GET_CHAT_MESSAGES_BASE + /{v}/read-by", "GET /chat/message/{messageId}/read-by") in got
    assert ("ChatApiConstant.GET_CHAT_MESSAGES_BASE + /{v}", "GET /chat/message/{messageId}") in got
    assert not any(a.endswith("+ /unread-count") and b == "GET /chat/message/{messageId}" for a, b in got)
    # a `${id}` UI segment goes to the endpoint's `{var}`, not to the literal `unread-count`
    assert not any(a.endswith("+ /{v}") and b == "GET /chat/message/unread-count" for a, b in got)
    assert not any("+ /" == a[-3:] for a, _ in got)                     # `BASE + '/'` has no suffix segment: no node

    used = db.conn.execute(
        """SELECT f.file_path FROM edges e JOIN nodes f ON f.id = e.source_id JOIN nodes t ON t.id = e.target_id
           WHERE e.relation = 'USES_API_CONSTANT' AND t.name = 'ChatApiConstant.GET_CHAT_MESSAGES_BASE + /unread-count'""").fetchall()
    assert [r[0] for r in used] == ["src/lib/chat-api.service.ts"]       # one node per (base, suffix, file)


def test_derived_constant_without_a_known_base_stays_unresolved(tmp_path):
    db = _build_chat(tmp_path)
    db.conn.execute("DELETE FROM nodes WHERE node_type='API_CONSTANT' AND name = 'ChatApiConstant.GET_CHAT_MESSAGES_BASE'")
    db.resolve_api_constant_edges({"supplementary": "supplementary-service"})
    res = {json.loads(m)["resolution"] for (m,) in db.conn.execute(
        "SELECT metadata FROM nodes WHERE node_type='API_CONSTANT' AND name LIKE '%+ /unread-count'")}
    assert res == {"base_constant_not_found"}


def test_entry_endpoint_prefers_the_literal_mapping_over_a_wildcard(tmp_path):
    from mcp_server.queries.saga_queries import find_entry_endpoints
    db = _build_chat(tmp_path)
    db.conn.row_factory = sqlite3.Row
    found = find_entry_endpoints(db.conn, None, "GET", "/chat/message/unread-count")
    assert [e["path"] for e in found] == ["/chat/message/unread-count"]
    found = find_entry_endpoints(db.conn, None, "GET", "/chat/message/42")
    assert [e["path"] for e in found] == ["/chat/message/{messageId}"]
