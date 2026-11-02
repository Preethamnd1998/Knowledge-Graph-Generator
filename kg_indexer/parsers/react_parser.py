"""Regex-based React parser: function/class components, custom hooks,
hook usage inside a component, JSX composition (RENDERS), and exports.
"""
import os
import re

from .common import (
    API_CONSTANT_USAGE_RE, NameRegistry, PLAIN_CLASS_RE, STATIC_STRING_FIELD_RE,
    emit_suffix_constant_usages, find_matching, is_api_constant_class_name, line_of, normalize_ui_path,
    read_text, walk_source_files,
)

COMPONENT_FUNC_RE = re.compile(r'\bfunction\s+(?P<name>[A-Z]\w*)\s*\([^)]*\)\s*(?::\s*[\w.<>\[\], |]+?)?\s*\{')
COMPONENT_ARROW_RE = re.compile(
    r'\b(?:export\s+)?const\s+(?P<name>[A-Z]\w*)\s*(?::\s*[^=]+?)?=\s*'
    r'(?:React\.memo\(|React\.forwardRef\(|memo\(|forwardRef\()?\s*'
    r'\([^)]*\)\s*(?::\s*[\w.<>\[\], |]+?)?\s*=>\s*\{'
)
CLASS_COMPONENT_RE = re.compile(
    r'\bclass\s+(?P<name>[A-Z]\w*)\s+extends\s+(?:React\.)?(?:PureComponent|Component)\b[^{]*\{'
)
HOOK_FUNC_RE = re.compile(r'\bfunction\s+(?P<name>use[A-Z]\w*)\s*\([^)]*\)\s*(?::\s*[\w.<>\[\], |]+?)?\s*\{')
HOOK_ARROW_RE = re.compile(
    r'\b(?:export\s+)?const\s+(?P<name>use[A-Z]\w*)\s*=\s*\([^)]*\)\s*'
    r'(?::\s*[\w.<>\[\], |]+?)?\s*=>\s*\{'
)

HOOK_CALL_RE = re.compile(r'\b(use[A-Z]\w*)\s*\(')
JSX_TAG_RE = re.compile(r'<([A-Z]\w*)[\s/>]')

EXPORT_DEFAULT_NAME_RE = re.compile(r'export\s+default\s+(?:function\s+|class\s+)?(\w+)')
EXPORT_NAMED_DECL_RE = re.compile(r'export\s+(?:const|function|class)\s+(\w+)')
EXPORT_LIST_RE = re.compile(r'export\s*\{([^}]*)\}')

ES_IMPORT_RE = re.compile(
    r'import\s+(?:type\s+)?(?:\{(?P<named>[^}]*)\}|(?P<default>\w+))\s+from\s+'
    r'[\'"](?P<module>[^\'"]+)[\'"]'
)


def _exported_names(text):
    names = set()
    m = EXPORT_DEFAULT_NAME_RE.search(text)
    if m:
        names.add(m.group(1))
    names.update(EXPORT_NAMED_DECL_RE.findall(text))
    for m in EXPORT_LIST_RE.finditer(text):
        for n in m.group(1).split(","):
            n = n.strip().split(" as ")[0].strip()
            if n:
                names.add(n)
    return names


def _find_defs(text, patterns, kind_by_pattern):
    seen_spans = set()
    defs = []
    for pattern in patterns:
        for m in pattern.finditer(text):
            if m.start() in seen_spans:
                continue
            seen_spans.add(m.start())
            defs.append((m.group("name"), m.start(), m.end() - 1, kind_by_pattern[pattern]))
    defs.sort(key=lambda d: d[1])
    return defs


def _parse_file(db, registry, project_name, project_root, file_path, service_node_id, stats):
    text = read_text(file_path)
    if text is None:
        stats["errors"] += 1
        return

    rel_file = os.path.relpath(file_path, project_root)
    file_qname = f"react:{project_name}:{rel_file}"
    file_node_id = db.add_node(
        "FILE", os.path.basename(file_path), qualified_name=file_qname,
        service=project_name, project_type="react", file_path=rel_file,
    )
    db.add_edge(service_node_id, "CONTAINS", target_id=file_node_id, service=project_name)
    stats["files"] += 1

    exported = _exported_names(text)

    for m in ES_IMPORT_RE.finditer(text):
        module = m.group("module")
        if not module.startswith("."):
            continue
        names = [n.strip().split(" as ")[0].strip() for n in m.group("named").split(",")] \
            if m.group("named") else [m.group("default")]
        for n in names:
            if n:
                db.add_edge(file_node_id, "IMPORTS", target_name=n,
                            service=project_name, metadata={"module": module})

    component_defs = _find_defs(
        text, [COMPONENT_FUNC_RE, COMPONENT_ARROW_RE, CLASS_COMPONENT_RE],
        {COMPONENT_FUNC_RE: "function", COMPONENT_ARROW_RE: "function", CLASS_COMPONENT_RE: "class"},
    )
    hook_defs = _find_defs(
        text, [HOOK_FUNC_RE, HOOK_ARROW_RE],
        {HOOK_FUNC_RE: "function", HOOK_ARROW_RE: "function"},
    )
    hook_names_defined = {d[0] for d in hook_defs}
    component_defs = [d for d in component_defs if d[0] not in hook_names_defined]

    for name, start, brace_index, kind in component_defs:
        try:
            _emit(db, registry, project_name, rel_file, text, name, start, brace_index, kind,
                  "REACT_COMPONENT", exported, file_node_id, stats, jsx_scan=True)
        except Exception:
            stats["errors"] += 1

    for name, start, brace_index, kind in hook_defs:
        try:
            _emit(db, registry, project_name, rel_file, text, name, start, brace_index, kind,
                  "REACT_HOOK", exported, file_node_id, stats, jsx_scan=False)
        except Exception:
            stats["errors"] += 1

    for pm in PLAIN_CLASS_RE.finditer(text):
        if not is_api_constant_class_name(pm.group("name")):
            continue
        try:
            _parse_api_constant_class(db, registry, project_name, rel_file, text, pm,
                                       file_node_id, stats)
        except Exception:
            stats["errors"] += 1

    for um in API_CONSTANT_USAGE_RE.finditer(text):
        key = f"{um.group('cls')}.{um.group('field')}"
        db.add_edge(file_node_id, "USES_API_CONSTANT", target_name=key, service=project_name)
    stats["API_CONSTANT"] = stats.get("API_CONSTANT", 0) + emit_suffix_constant_usages(
        db, "react", project_name, project_name, rel_file, text, file_node_id)


def _emit(db, registry, project_name, rel_file, text, name, start, brace_index, kind,
          node_type, exported_names, file_node_id, stats, jsx_scan):
    body_end = find_matching(text, brace_index, "{", "}")
    body_text = text[brace_index:body_end]
    start_line = line_of(text, start)
    end_line = line_of(text, body_end)
    qname = f"react:{project_name}:{rel_file}:{name}"

    node_id = db.add_node(
        node_type, name, qualified_name=qname, service=project_name,
        project_type="react", file_path=rel_file, start_line=start_line,
        end_line=end_line, metadata={"kind": kind, "exported": name in exported_names},
    )
    registry.register(name, qname)
    db.add_edge(file_node_id, "DECLARES", target_id=node_id, service=project_name)
    stats["components" if node_type == "REACT_COMPONENT" else "hooks"] = (
        stats.get("components" if node_type == "REACT_COMPONENT" else "hooks", 0) + 1
    )

    for hook_name in sorted(set(HOOK_CALL_RE.findall(body_text))):
        db.add_edge(node_id, "USES_HOOK", target_name=hook_name, service=project_name)

    if jsx_scan:
        for tag in sorted(set(JSX_TAG_RE.findall(body_text)) - {name}):
            db.add_edge(node_id, "RENDERS", target_name=tag, service=project_name)


def _parse_api_constant_class(db, registry, project_name, rel_file, text, pm,
                               file_node_id, stats):
    """Same `FooApiConstant` field-level detection as angular_parser.py's
    _parse_api_constant_class — kept as a near-duplicate here rather than a
    shared function because the two languages' qualified_name schemes
    differ (no subproject segment for React) and this parser has no real
    workspace target to exercise it against yet (REACT_PROJECT_PATH is
    unset in kg-config.env); see common.py for the actually-shared regexes."""
    class_name = pm.group("name")
    body_start = pm.end() - 1
    body_end = find_matching(text, body_start, "{", "}")
    body_text = text[body_start:body_end]

    count = 0
    for fm in STATIC_STRING_FIELD_RE.finditer(body_text):
        field_name = fm.group("field")
        raw_value = fm.group("dq") if fm.group("dq") is not None else fm.group("sq")
        normalized_path, segments = normalize_ui_path(raw_value)
        qname = f"react:{project_name}:{rel_file}:{class_name}.{field_name}"
        field_line = line_of(text, body_start + fm.start())
        node_id = db.add_node(
            "API_CONSTANT", f"{class_name}.{field_name}", qualified_name=qname,
            service=project_name, project_type="react", file_path=rel_file,
            start_line=field_line,
            metadata={
                "class_name": class_name, "field_name": field_name, "raw_value": raw_value,
                "normalized_path": normalized_path, "segments": segments,
            },
        )
        registry.register(f"{class_name}.{field_name}", qname)
        db.add_edge(file_node_id, "DECLARES", target_id=node_id, service=project_name)
        count += 1
    stats["API_CONSTANT"] = stats.get("API_CONSTANT", 0) + count


def parse_project(db, registry: NameRegistry, project_root, progress=None, only_files=None):
    project_name = os.path.basename(project_root.rstrip("\\/")) or "react-app"
    stats = {"files": 0, "components": 0, "hooks": 0, "errors": 0}

    service_node_id = db.add_node(
        "SERVICE", project_name, qualified_name=f"react:{project_name}",
        service=project_name, project_type="react", file_path=project_root,
    )

    for file_path in walk_source_files(
        project_root, (".jsx", ".tsx", ".js", ".ts"),
        ignored_suffixes=(".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx", ".d.ts"),
    ):
        if only_files is not None and file_path not in only_files:
            continue
        _parse_file(db, registry, project_name, project_root, file_path, service_node_id, stats)
        if progress:
            progress(project_name, file_path, stats)

    return stats, project_name
