"""DEPENDS_ON edges for constructor-injected (`private final X x;`) classes."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_spring_flow_parser import _index, _write

SERVICE = """
package com.acme.settings.service.impl;

import org.slf4j.Logger;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

@Service
public class SettingsServiceImpl implements SettingsService {

    private static final Logger LOGGER = LoggerFactory.getLogger(SettingsServiceImpl.class);
    private static final String NAME = "x";

    private final CTAService ctaService;
    private final RuleDaoService ruleDaoService;
    private final String label;
    private final java.util.List<String> names;

    @Autowired
    private final AuditService auditService;

    public SettingsServiceImpl(CTAService ctaService, RuleDaoService ruleDaoService) {
        this.ctaService = ctaService;
        this.ruleDaoService = ruleDaoService;
    }
}
"""

ENTITY = """
package com.acme.settings.domain;

import javax.persistence.Entity;

@Entity
public class Rule {
    private final Owner owner;
}
"""

DTO = """
package com.acme.settings.dto;

public class RuleDTO {
    private final Owner owner;
}
"""


def _deps(db, class_name):
    rows = db.conn.execute(
        """SELECT e.target_name, e.metadata FROM edges e JOIN nodes s ON s.id = e.source_id
           WHERE e.relation = 'DEPENDS_ON' AND s.name = ?""", (class_name,)).fetchall()
    return {(r["target_name"], json.loads(r["metadata"] or "{}").get("via")) for r in rows}


def test_constructor_injection_becomes_depends_on_and_noise_is_excluded(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/settings/service/impl/SettingsServiceImpl.java", SERVICE)
    _write(svc, "src/main/java/com/acme/settings/domain/Rule.java", ENTITY)
    _write(svc, "src/main/java/com/acme/settings/dto/RuleDTO.java", DTO)
    db, _ = _index(tmp_path, "ci.db", "BE/settings", svc)

    deps = _deps(db, "SettingsServiceImpl")
    assert ("CTAService", "ConstructorInjection") in deps
    assert ("RuleDaoService", "ConstructorInjection") in deps
    assert ("AuditService", "Autowired") in deps                                   # not double-counted
    assert not any(t == "AuditService" and via == "ConstructorInjection" for t, via in deps)
    assert not any(t in ("Logger", "String", "List") for t, _ in deps)            # static/JDK/value types
    assert _deps(db, "Rule") == set()                                              # @Entity: data, not wiring
    assert _deps(db, "RuleDTO") == set()


CALLER = """
package com.acme.settings.service.impl;

public class RuleServiceImpl implements RuleService {

    private final CTAService ctaService;
    private final RuleRepository ruleRepository;
    private final RuleMapper mapper;

    public void save(Rule r) {
        Object cta = this.ctaService.getCustomerTransactionAttribute(r);
        ruleRepository.getRule(r.getId());
        mapper.toDto(r);
        local.helper();
        ctaService.toString();
        ctaService.getCustomerTransactionAttribute(r);
    }
}
"""


def test_calls_on_injected_fields_become_typed_call_edges(tmp_path):
    svc = tmp_path / "svc"
    _write(svc, "src/main/java/com/acme/settings/service/impl/RuleServiceImpl.java", CALLER)
    db, _ = _index(tmp_path, "cd.db", "BE/settings", svc)

    rows = db.conn.execute(
        """SELECT e.target_name, e.metadata FROM edges e JOIN nodes s ON s.id = e.source_id
           WHERE e.relation = 'CALLS_DEP' AND s.name = 'save'""").fetchall()
    targets = {r["target_name"] for r in rows}
    assert targets == {"CTAService#getCustomerTransactionAttribute", "RuleRepository#getRule", "RuleMapper#toDto"}
    # `local.helper()` (not a field), `toString()` (skip list) and the duplicate call are not edges.
    meta = json.loads(next(r["metadata"] for r in rows if r["target_name"].startswith("CTAService")))
    assert meta["field"] == "ctaService" and meta["type"] == "CTAService" and meta["confidence"] == "verified"
