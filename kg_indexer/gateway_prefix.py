"""Gateway-prefix resolution for backend services.

Spring Cloud Gateway's discovery locator routes `/{registered-name}/**` to the
service that registered `{registered-name}` — its `spring.application.name`.
The KG stores that name on each SERVICE node (`metadata.gateway_prefix`); the
indexer's Feign/UI-constant resolution and the saga URL resolver both read it.
(The repo holds no gateway route table — there are no route ids/URIs to index;
the prefix IS the registered application name.)

Some services declare the name as a placeholder (`${SPRING_APPLICATION_NAME}`,
injected by the deployment) or not at all, so it cannot be read from source.
Resolution order, first hit wins, and the SOURCE is always recorded:

  1. literal      `spring.application.name: core`                (verified source)
  2. env          `${NAME}` / `${NAME:default}` and NAME is set in the indexing env
  3. placeholder-default  `${NAME:default}` default value
  4. override     explicit entry in gateway-prefix-overrides.json (documented,
                  evidence-backed, central — never scattered through the code)
  5. unresolved   no prefix is invented; a warning (or error with
                  KG_GATEWAY_STRICT=1) is reported

A literal/env value that disagrees with an override is a CONFLICT: the source
value wins and a warning is raised. Folder names are never used as a prefix.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

SOURCE_LITERAL = "literal"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "placeholder-default"
SOURCE_OVERRIDE = "override"

OVERRIDES_ENV_VAR = "KG_GATEWAY_PREFIX_OVERRIDES"
STRICT_ENV_VAR = "KG_GATEWAY_STRICT"
DEFAULT_OVERRIDES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway-prefix-overrides.json"
)

CONFIG_CANDIDATES = (
    "bootstrap.yml", "bootstrap.yaml", "application.yml", "application.yaml", "application.properties",
)
_APP_NAME_YAML_RE = re.compile(
    r'application\s*:\s*\r?\n((?:[ \t]*(?:#.*)?\r?\n)*)[ \t]+name\s*:\s*([^\s#]*)'
)
_APP_NAME_PROPS_RE = re.compile(r'^\s*spring\.application\.name\s*=\s*([^\s#]*)', re.MULTILINE)
_WHOLE_PLACEHOLDER_RE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_.\-]*)(?::([^}]*))?\}$')
_PREFIX_RE = re.compile(r'^[a-z0-9][a-z0-9\-]*$')


@dataclass
class GatewayPrefix:
    prefix: str | None
    source: str | None
    status: str                      # "resolved" | "unresolved"
    warnings: list = field(default_factory=list)   # problems: conflict / unresolved / invalid (strict mode fails on these)
    notes: list = field(default_factory=list)      # informational: e.g. an explicit override was used
    declared: str | None = None                    # raw value found in config (may be a placeholder)


class OverrideError(ValueError):
    pass


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def declared_names(service_path):
    """[(file, raw_value)] for every candidate config that declares a name
    (raw_value may be '' when declared empty). Only src/main/resources is read."""
    res = os.path.join(service_path, "src", "main", "resources")
    out = []
    if not os.path.isdir(res):
        return out
    for fname in CONFIG_CANDIDATES:
        text = _read(os.path.join(res, fname))
        if not text:
            continue
        m = _APP_NAME_YAML_RE.search(text) or _APP_NAME_PROPS_RE.search(text)
        if m:
            out.append((fname, m.group(m.lastindex).strip().strip("'\"")))
    return out


def load_overrides(path=None, env=None):
    """{service-key -> {"prefix", "evidence"}} from the central overrides file.

    A missing DEFAULT file means "no overrides"; a missing explicitly-requested
    file (env var / argument) is an error. Entries are validated: lowercase
    prefix, and no two services may share an override prefix."""
    env = os.environ if env is None else env
    explicit = path or env.get(OVERRIDES_ENV_VAR)
    f = explicit or DEFAULT_OVERRIDES_FILE
    if not os.path.isfile(f):
        if explicit:
            raise OverrideError(f"gateway prefix overrides file not found: {f}")
        return {}
    try:
        data = json.loads(_read(f))
    except json.JSONDecodeError as e:
        raise OverrideError(f"{f}: invalid JSON ({e})") from e
    raw = data.get("overrides", {})
    if not isinstance(raw, dict):
        raise OverrideError(f"{f}: 'overrides' must be an object")
    out, seen = {}, {}
    for key, val in raw.items():
        prefix = val.get("prefix") if isinstance(val, dict) else val
        evidence = val.get("evidence", "") if isinstance(val, dict) else ""
        if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
            raise OverrideError(f"{f}: override for {key!r} has invalid prefix {prefix!r} (lowercase letters/digits/-)")
        if prefix in seen:
            raise OverrideError(f"{f}: services {seen[prefix]!r} and {key!r} share override prefix {prefix!r}")
        seen[prefix] = key
        out[key] = {"prefix": prefix, "evidence": evidence}
    return out


def _override_for(service_name, overrides):
    if not overrides:
        return None
    if service_name in overrides:
        return overrides[service_name]
    base = service_name.rsplit("/", 1)[-1]
    return overrides.get(base)


def resolve_gateway_prefix(service_name, service_path, overrides=None, env=None):
    env = os.environ if env is None else env
    overrides = {} if overrides is None else overrides
    ov = _override_for(service_name, overrides)
    names = declared_names(service_path)
    warnings, notes = [], []
    value = source = None

    declared = next((v for _f, v in names), None)
    # 1. first literal anywhere wins (matches the original multi-file behaviour)
    for _f, raw in names:
        if raw and "${" not in raw:
            value, source = raw.lower(), SOURCE_LITERAL
            break
    # 2./3. placeholders
    if value is None:
        for _f, raw in names:
            m = _WHOLE_PLACEHOLDER_RE.match(raw or "")
            if not m:
                continue
            var, default = m.group(1), m.group(2)
            if env.get(var):
                value, source = env[var].strip().lower(), SOURCE_ENV
                break
            if default:
                value, source = default.strip().lower(), SOURCE_DEFAULT
                break
    # 4. explicit override (also covers missing / empty / unresolvable names)
    if value is None and ov:
        value, source = ov["prefix"], SOURCE_OVERRIDE
        why = "no spring.application.name" if not names else (
            "empty spring.application.name" if declared == "" else f"unresolvable name {declared!r}")
        notes.append(f"{service_name}: {why}; using explicit override {value!r}"
                        + (f" (evidence: {ov['evidence']})" if ov.get("evidence") else ""))
    # conflict: a real source value disagrees with an override
    if value is not None and source != SOURCE_OVERRIDE and ov and ov["prefix"] != value:
        warnings.append(f"{service_name}: CONFLICT — {source} value {value!r} differs from override "
                        f"{ov['prefix']!r}; keeping the {source} value")
    if value is not None and not _PREFIX_RE.match(value):
        warnings.append(f"{service_name}: resolved prefix {value!r} is not a valid gateway name; ignored")
        value = source = None
    if value is None:
        reason = ("no config declares spring.application.name" if not names else
                  "spring.application.name is empty" if declared == "" else
                  f"spring.application.name is unresolved placeholder {declared!r}")
        warnings.append(f"{service_name}: gateway prefix UNRESOLVED — {reason} and no override is configured "
                        f"(add it to gateway-prefix-overrides.json); gateway URLs/Feign calls to this service will not resolve")
        return GatewayPrefix(None, None, "unresolved", warnings, notes, declared)
    return GatewayPrefix(value, source, "resolved", warnings, notes, declared)


def find_duplicates(prefix_by_service):
    """{prefix: [services...]} for prefixes claimed by more than one service."""
    by = {}
    for svc, prefix in prefix_by_service.items():
        if prefix:
            by.setdefault(prefix, []).append(svc)
    return {p: s for p, s in by.items() if len(s) > 1}


def strict_mode(env=None):
    env = os.environ if env is None else env
    return str(env.get(STRICT_ENV_VAR, "")).lower() in ("1", "true", "yes")
