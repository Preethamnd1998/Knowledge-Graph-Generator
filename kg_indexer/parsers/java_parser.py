"""Regex-based Java parser: classes/interfaces/enums, methods, REST
endpoints (@*Mapping), and dependency edges (extends/implements/@Autowired).
"""
import os
import re

from .common import (
    NameRegistry, is_api_constant_class_name, line_of, read_text, strip_type,
    walk_source_files,
)

PACKAGE_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.MULTILINE)
IMPORT_RE = re.compile(r'^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;', re.MULTILINE)

ANNOTATION_RE = re.compile(r'@(\w[\w.]*)(\((?:[^()]|\([^()]*\))*\))?')

CLASS_DECL_RE = re.compile(
    r'(?P<annotations>(?:@\w[\w.]*(?:\((?:[^()]|\([^()]*\))*\))?\s*)*)'
    r'(?:public\s+|private\s+|protected\s+|static\s+|final\s+|abstract\s+|strictfp\s+)*'
    r'(?P<kind>class|interface|enum|record)\s+'
    r'(?P<name>\w+)'
    r'(?:\s*<[^{;]*?>)?'
    r'(?:\s*\([^)]*\))?'
    r'(?:\s+extends\s+(?P<extends>[^{;]+?))?'
    r'(?:\s+implements\s+(?P<implements>[^{;]+?))?'
    r'\s*\{',
)

METHOD_RE = re.compile(
    r'(?P<annotations>(?:@\w[\w.]*(?:\((?:[^()]|\([^()]*\))*\))?\s*(?:\r?\n[ \t]*)?)*)'
    r'(?:public|protected|private)\s+'
    r'(?:static\s+|final\s+|synchronized\s+|abstract\s+|default\s+|native\s+)*'
    r'(?:<[^{;]+?>\s*)?'
    r'(?P<return_type>[\w][\w<>,\[\].? ]*?)\s+'
    r'(?P<name>\w+)\s*'
    r'\((?P<params>(?:[^()]|\((?:[^()]|\([^()]*\))*\))*)\)'
    r'(?:\s*throws\s+[\w.,\s]+)?'
    r'\s*\{',
)

FIELD_DEP_RE = re.compile(
    r'@(Autowired|Inject|Resource)\b(?:\([^)]*\))?[^\n]*\n\s*'
    r'(?:private\s+|protected\s+|public\s+|final\s+)*'
    r'(?P<type>[\w.]+)(?:<[^;]*?>)?(?:\[\])?\s+'
    r'(?P<name>\w+)\s*[;=]'
)

HTTP_MAPPING_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}

QUOTED_STRING_RE = re.compile(r'"([^"]*)"')
REQUEST_METHOD_RE = re.compile(r'RequestMethod\.(\w+)')
PARAM_ANNOTATION_PREFIX_RE = re.compile(r'^(?:@\w[\w.]*(?:\((?:[^()]|\([^()]*\))*\))?\s+)+')

# --- API Gateway prefix (spring.application.name) --------------------------
#
# Spring Cloud Gateway's discovery-locator routes `/{registered-name}/**` to
# whichever service registered that name in Consul (see gateway-service's
# bootstrap.yml: `discovery.locator.enabled: true`). That registered name is
# each service's own `spring.application.name` — e.g. ipd-service registers
# as "ipd", so a UI call to "ipd/opd/confirm/payment" is gateway-routed to
# ipd-service with "/opd/confirm/payment" as the path the controller sees.
# Read directly off each service's own config (never hand-mapped/hardcoded
# in this file — that would violate the "never guess" rule and break the
# moment a service's registered name changes) so this ports to any codebase
# using the same discovery-locator convention with zero code changes.
_APP_NAME_YAML_RE = re.compile(
    r'application\s*:\s*\r?\n((?:[ \t]*(?:#.*)?\r?\n)*)[ \t]+name\s*:\s*([^\s#]+)'
)
_APP_NAME_PROPS_RE = re.compile(r'^\s*spring\.application\.name\s*=\s*([^\s#]+)', re.MULTILINE)
_PLACEHOLDER_VALUE_RE = re.compile(r'^\$\{.*\}$')
_GATEWAY_CONFIG_CANDIDATES = (
    "bootstrap.yml", "bootstrap.yaml", "application.yml", "application.yaml",
    "application.properties",
)


def _extract_gateway_prefix(service_path):
    """Best-effort `spring.application.name` for one backend service, read
    from its own src/main/resources config (never recursed beyond that one
    directory — no bulk scan). Returns None (never a guess) if no config
    file declares it, or the declared value is an unresolved `${...}`
    placeholder (e.g. config-service's `${CONFIG_APP_NAME}`)."""
    resources_dir = os.path.join(service_path, "src", "main", "resources")
    if not os.path.isdir(resources_dir):
        return None
    for fname in _GATEWAY_CONFIG_CANDIDATES:
        fpath = os.path.join(resources_dir, fname)
        if not os.path.isfile(fpath):
            continue
        text = read_text(fpath)
        if not text:
            continue
        m = _APP_NAME_YAML_RE.search(text) or _APP_NAME_PROPS_RE.search(text)
        if not m:
            continue
        value = m.group(m.lastindex).strip().strip("'\"")
        if not value or _PLACEHOLDER_VALUE_RE.match(value):
            continue
        return value.lower()
    return None


# --- Feign clients (backend -> backend) -------------------------------------
#
# A @FeignClient interface's methods are declarations, not definitions —
# they end in ';', never '{', so the body-requiring METHOD_RE above never
# matches them (by design, not by accident). This mirrors METHOD_RE closely
# on purpose (same annotation/param-list handling) but with the access
# modifier optional (interface methods are implicitly public and commonly
# written with no modifier at all) and a ';' terminator instead of a body.
FEIGN_METHOD_RE = re.compile(
    r'(?P<annotations>(?:@\w[\w.]*(?:\((?:[^()]|\([^()]*\))*\))?\s*(?:\r?\n[ \t]*)?)*)'
    r'(?:public\s+|protected\s+|private\s+|default\s+|static\s+)*'
    r'(?:<[^{;]+?>\s*)?'
    r'(?P<return_type>[\w][\w<>,\[\].? ]*?)\s+'
    r'(?P<name>\w+)\s*'
    r'\((?P<params>(?:[^()]|\((?:[^()]|\([^()]*\))*\))*)\)'
    r'(?:\s*throws\s+[\w.,\s]+)?'
    r'\s*;'
)
FEIGN_NAME_RE = re.compile(r'\b(?:name|value)\s*=\s*"([^"]+)"')
FEIGN_BARE_STRING_RE = re.compile(r'^\s*"([^"]+)"\s*(?:,|$)')
FEIGN_URL_RE = re.compile(r'\burl\s*=')


def _feign_client_name(args_text):
    if not args_text:
        return None
    m = FEIGN_NAME_RE.search(args_text)
    if m:
        return m.group(1)
    m = FEIGN_BARE_STRING_RE.match(args_text)
    return m.group(1) if m else None


def _parse_annotations(annotation_blob):
    return [
        {"name": m.group(1).rsplit(".", 1)[-1], "args": m.group(2) or ""}
        for m in ANNOTATION_RE.finditer(annotation_blob or "")
    ]


SYMBOLIC_MAPPING_VALUE_RE = re.compile(
    r'\b(?:value|path)\s*=\s*([A-Za-z_]\w*\.[A-Za-z_]\w*)|^\s*([A-Za-z_]\w*\.[A-Za-z_]\w*)\s*(?:,|$)'
)


def _annotation_path(args, java_api_constants=None):
    """`@GetMapping("/literal/path")` is the common case (a quoted string
    literal). Some controllers in this codebase instead write
    `@PostMapping(value = BillingApiConstant.CONFIRM_PAYMENT)` — a symbolic
    reference to a `public static final String` field on a same-service
    "ApiConstant" class (the same naming convention angular_parser.py
    recognizes on the UI side). `java_api_constants` (built once per service
    by _collect_java_api_constants, see parse_service) resolves that
    reference to its literal value — an exact compile-time-constant lookup,
    not a guess; a reference that isn't found there is left unresolved."""
    if not args:
        return None
    m = QUOTED_STRING_RE.search(args)
    if m:
        return m.group(1)
    if java_api_constants:
        sm = SYMBOLIC_MAPPING_VALUE_RE.search(args)
        if sm:
            ref = sm.group(1) or sm.group(2)
            return java_api_constants.get(ref)
    return None


JAVA_STRING_CONSTANT_RE = re.compile(
    r'public\s+static\s+final\s+String\s+(?P<field>[A-Za-z_]\w*)\s*=\s*"(?P<value>[^"]*)"\s*;'
)


def _collect_java_api_constants(service_path):
    """Pre-pass (regex-only, no node/edge creation) over one backend
    service's own source tree: {"ClassName.FIELD": "literal value"} for
    every `public static final String FIELD = "...";` inside a class whose
    name matches the *ApiConstant(s) convention (is_api_constant_class_name,
    shared with angular_parser.py). Run once per service before its main
    parse pass so @*Mapping(value = SomeApiConstant.FIELD) can be resolved
    — see _annotation_path. Scoped to this one service (never cross-service:
    Java compile-time constants can't be referenced across separate Maven
    build artifacts in this architecture)."""
    constants = {}
    for file_path in walk_source_files(service_path, (".java",), ignored_suffixes=("Test.java", "Tests.java")):
        text = read_text(file_path)
        if not text:
            continue
        for cm in CLASS_DECL_RE.finditer(text):
            name = cm.group("name")
            if not is_api_constant_class_name(name):
                continue
            body_start = cm.end() - 1
            body_end = _find_block_end(text, body_start)
            body_text = text[body_start:body_end]
            for fm in JAVA_STRING_CONSTANT_RE.finditer(body_text):
                constants[f"{name}.{fm.group('field')}"] = fm.group("value")
    return constants


def _join_path(base, sub):
    parts = [p.strip("/") for p in (base, sub) if p]
    joined = "/".join(p for p in parts if p)
    return "/" + joined if joined else "/"


def _split_types(blob):
    """Split a comma-separated type list ("Foo, Bar<Baz, Qux>") respecting
    angle-bracket nesting so generic type args aren't treated as separators.
    """
    if not blob:
        return []
    parts, depth, current = [], 0, []
    for ch in blob:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _find_block_end(text, open_brace_index):
    """Return the index just after the '}' matching the '{' at
    open_brace_index, skipping braces inside string/char literals and
    comments so nested classes/methods don't confuse the count."""
    depth = 0
    i = open_brace_index
    n = len(text)
    in_str = in_char = in_line_comment = in_block_comment = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 1
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif in_char:
            if ch == "\\":
                i += 1
            elif ch == "'":
                in_char = False
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 1
            elif ch == "/" and nxt == "*":
                in_block_comment = True
                i += 1
            elif ch == '"':
                in_str = True
            elif ch == "'":
                in_char = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return n


NODE_TYPE_BY_KIND = {
    "class": "CLASS",
    "interface": "INTERFACE",
    "enum": "ENUM",
    "record": "CLASS",
}


def _parse_file(db, registry, service_name, service_path, file_path, service_node_id, stats,
                 java_api_constants=None):
    text = read_text(file_path)
    if text is None:
        stats["errors"] += 1
        return

    rel_file = os.path.relpath(file_path, service_path)
    pkg_match = PACKAGE_RE.search(text)
    package = pkg_match.group(1) if pkg_match else ""

    file_qname = f"java:{service_name}:{rel_file}"
    file_node_id = db.add_node(
        "FILE", os.path.basename(file_path), qualified_name=file_qname,
        service=service_name, project_type="java", file_path=rel_file,
    )
    db.add_edge(service_node_id, "CONTAINS", target_id=file_node_id, service=service_name)
    stats["files"] += 1

    for m in IMPORT_RE.finditer(text):
        imported = m.group(1)
        if imported.endswith(".*"):
            continue
        simple = imported.rsplit(".", 1)[-1]
        db.add_edge(
            file_node_id, "IMPORTS", target_name=simple, service=service_name,
            metadata={"import_path": imported},
        )

    for cm in CLASS_DECL_RE.finditer(text):
        try:
            _parse_class(db, registry, service_name, rel_file, text, cm,
                         package, file_node_id, stats, java_api_constants)
        except Exception:
            stats["errors"] += 1


def _parse_class(db, registry, service_name, rel_file, text, cm, package,
                  file_node_id, stats, java_api_constants=None):
    kind = cm.group("kind")
    name = cm.group("name")
    qname = f"{package}.{name}" if package else name
    annotations = _parse_annotations(cm.group("annotations"))
    annotation_names = [a["name"] for a in annotations]

    body_start = cm.end() - 1  # index of the opening '{'
    body_end = _find_block_end(text, body_start)
    body_text = text[body_start:body_end]

    start_line = line_of(text, cm.start())
    end_line = line_of(text, body_end)

    node_type = NODE_TYPE_BY_KIND.get(kind, "CLASS")
    class_node_id = db.add_node(
        node_type, name, qualified_name=qname, service=service_name,
        project_type="java", file_path=rel_file, start_line=start_line,
        end_line=end_line, signature=re.sub(r"\s+", " ", cm.group(0)).strip()[:300],
        metadata={"annotations": annotation_names, "package": package},
    )
    registry.register(name, qname)
    db.add_edge(file_node_id, "DECLARES", target_id=class_node_id, service=service_name)
    stats["classes"] += 1

    for token in _split_types(cm.group("extends")):
        db.add_edge(class_node_id, "EXTENDS", target_name=strip_type(token), service=service_name)
    for token in _split_types(cm.group("implements")):
        db.add_edge(class_node_id, "IMPLEMENTS", target_name=strip_type(token), service=service_name)

    base_path = ""
    for a in annotations:
        if a["name"] == "RequestMapping":
            base_path = _annotation_path(a["args"], java_api_constants) or ""

    for fm in FIELD_DEP_RE.finditer(body_text):
        dep_type = strip_type(fm.group("type"))
        if dep_type in ("String", "int", "long", "boolean", "double", "float", "Integer", "Long", "Boolean"):
            continue
        db.add_edge(
            class_node_id, "DEPENDS_ON", target_name=dep_type, service=service_name,
            metadata={"via": fm.group(1), "field_name": fm.group("name")},
        )

    for mm in METHOD_RE.finditer(body_text):
        _parse_method(db, service_name, rel_file, text, body_start, mm,
                       class_node_id, qname, base_path, stats, java_api_constants)

    feign_annotation = next((a for a in annotations if a["name"] == "FeignClient"), None)
    if feign_annotation is not None:
        feign_name = _feign_client_name(feign_annotation["args"])
        has_external_url = bool(FEIGN_URL_RE.search(feign_annotation["args"] or ""))
        for mm in FEIGN_METHOD_RE.finditer(body_text):
            _parse_feign_method(db, service_name, rel_file, text, body_start, mm,
                                 class_node_id, qname, base_path, feign_name,
                                 has_external_url, stats, java_api_constants)


def _parse_method(db, service_name, rel_file, full_text, body_start, mm,
                   class_node_id, class_qname, base_path, stats, java_api_constants=None):
    name = mm.group("name")
    return_type = mm.group("return_type").strip()
    params_raw = mm.group("params").strip()
    param_types = []
    for param in _split_types(params_raw):
        param = param.strip()
        if not param:
            continue
        param = param.replace("final ", "")
        param = PARAM_ANNOTATION_PREFIX_RE.sub("", param)
        pieces = param.rsplit(" ", 1)
        param_types.append(strip_type(pieces[0]) if len(pieces) > 1 else strip_type(param))

    line_no = line_of(full_text, body_start + mm.start())
    method_qname = f"{class_qname}#{name}({','.join(t or '' for t in param_types)})"
    annotations = _parse_annotations(mm.group("annotations"))

    method_node_id = db.add_node(
        "METHOD", name, qualified_name=method_qname, service=service_name,
        project_type="java", file_path=rel_file, start_line=line_no,
        signature=f"{return_type} {name}({params_raw})".strip()[:300],
        metadata={
            "return_type": strip_type(return_type),
            "params": param_types,
            "annotations": [a["name"] for a in annotations],
        },
    )
    db.add_edge(class_node_id, "CONTAINS", target_id=method_node_id, service=service_name)
    stats["methods"] += 1

    http_method = None
    sub_path = ""
    for a in annotations:
        if a["name"] in HTTP_MAPPING_ANNOTATIONS:
            http_method = HTTP_MAPPING_ANNOTATIONS[a["name"]]
            sub_path = _annotation_path(a["args"], java_api_constants) or ""
        elif a["name"] == "RequestMapping":
            rm = REQUEST_METHOD_RE.search(a["args"] or "")
            http_method = rm.group(1) if rm else "REQUEST"
            sub_path = _annotation_path(a["args"], java_api_constants) or ""

    if http_method:
        full_path = _join_path(base_path, sub_path)
        endpoint_qname = f"ENDPOINT:{service_name}:{http_method}:{full_path}"
        endpoint_node_id = db.add_node(
            "ENDPOINT", f"{http_method} {full_path}", qualified_name=endpoint_qname,
            service=service_name, project_type="java", file_path=rel_file,
            start_line=line_no, metadata={"http_method": http_method, "path": full_path},
        )
        db.add_edge(method_node_id, "EXPOSES", target_id=endpoint_node_id, service=service_name)
        stats["endpoints"] += 1


def _parse_feign_method(db, service_name, rel_file, full_text, body_start, mm,
                         class_node_id, class_qname, base_path, feign_name,
                         has_external_url, stats, java_api_constants=None):
    """A @FeignClient interface method — the outgoing counterpart of
    _parse_method's ENDPOINT/EXPOSES: this method *calls* another service's
    endpoint rather than serving one, so it gets a METHOD node (for
    consistency/queryability) plus a FEIGN_CALL edge instead of an ENDPOINT
    node. Left with only `target_name` set here — GraphDB.resolve_feign_edges
    does the actual cross-service resolution once every backend service's
    gateway prefix is known (may not be true yet mid-run)."""
    name = mm.group("name")
    return_type = mm.group("return_type").strip()
    params_raw = mm.group("params").strip()
    param_types = []
    for param in _split_types(params_raw):
        param = param.strip()
        if not param:
            continue
        param = param.replace("final ", "")
        param = PARAM_ANNOTATION_PREFIX_RE.sub("", param)
        pieces = param.rsplit(" ", 1)
        param_types.append(strip_type(pieces[0]) if len(pieces) > 1 else strip_type(param))

    line_no = line_of(full_text, body_start + mm.start())
    method_qname = f"{class_qname}#{name}({','.join(t or '' for t in param_types)})"
    annotations = _parse_annotations(mm.group("annotations"))

    method_node_id = db.add_node(
        "METHOD", name, qualified_name=method_qname, service=service_name,
        project_type="java", file_path=rel_file, start_line=line_no,
        signature=f"{return_type} {name}({params_raw})".strip()[:300],
        metadata={
            "return_type": strip_type(return_type),
            "params": param_types,
            "annotations": [a["name"] for a in annotations],
            "feign_client": True,
        },
    )
    db.add_edge(class_node_id, "CONTAINS", target_id=method_node_id, service=service_name)
    stats["methods"] += 1

    http_method = None
    sub_path = ""
    for a in annotations:
        if a["name"] in HTTP_MAPPING_ANNOTATIONS:
            http_method = HTTP_MAPPING_ANNOTATIONS[a["name"]]
            sub_path = _annotation_path(a["args"], java_api_constants) or ""
        elif a["name"] == "RequestMapping":
            rm = REQUEST_METHOD_RE.search(a["args"] or "")
            http_method = rm.group(1) if rm else "REQUEST"
            sub_path = _annotation_path(a["args"], java_api_constants) or ""

    if not http_method:
        return

    full_path = _join_path(base_path, sub_path)
    stats["feign_calls"] = stats.get("feign_calls", 0) + 1
    if has_external_url or not feign_name:
        db.add_edge(
            method_node_id, "FEIGN_CALL",
            target_name=f"external:{feign_name or '?'}:{http_method}:{full_path}",
            service=service_name,
            metadata={
                "target_prefix": feign_name, "http_method": http_method, "path": full_path,
                "confidence": "unknown", "external_url": has_external_url,
                "reason": "explicit @FeignClient(url=...) override or unnamed client — "
                          "bypasses service discovery, never resolved",
            },
        )
        return

    db.add_edge(
        method_node_id, "FEIGN_CALL",
        target_name=f"feign:{feign_name}:{http_method}:{full_path}",
        service=service_name,
        metadata={
            "target_prefix": feign_name, "http_method": http_method, "path": full_path,
            "confidence": "unknown",  # upgraded to "resolved" by GraphDB.resolve_service_call_edges
        },
    )


def parse_service(db, registry: NameRegistry, service_name, service_path, progress=None, only_files=None):
    stats = {"files": 0, "classes": 0, "methods": 0, "endpoints": 0, "errors": 0}

    gateway_prefix = _extract_gateway_prefix(service_path)
    java_api_constants = _collect_java_api_constants(service_path)
    service_node_id = db.add_node(
        "SERVICE", service_name, qualified_name=f"java:{service_name}",
        service=service_name, project_type="java", file_path=service_path,
        metadata={"gateway_prefix": gateway_prefix} if gateway_prefix else None,
    )

    for file_path in walk_source_files(service_path, (".java",), ignored_suffixes=("Test.java", "Tests.java")):
        if only_files is not None and file_path not in only_files:
            continue
        _parse_file(db, registry, service_name, service_path, file_path,
                    service_node_id, stats, java_api_constants)
        if progress:
            progress(service_name, file_path, stats)

    stats["gateway_prefix"] = gateway_prefix
    return stats
