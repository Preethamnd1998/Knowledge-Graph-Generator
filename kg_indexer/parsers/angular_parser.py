"""Regex-based Angular parser: @Component / @NgModule / @Injectable /
@Directive / @Pipe classes, module declarations/imports/exports,
constructor-injected dependencies, and local ES-module imports.
"""
import os
import re

from .common import (
    NameRegistry, find_matching, line_of, read_text, split_top_level,
    strip_type, walk_source_files,
)

DECORATOR_OPEN_RE = re.compile(r'@(Component|NgModule|Injectable|Directive|Pipe)\s*\(')
CLASS_HEADER_RE = re.compile(
    r'\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)'
    r'(?:\s+extends\s+(?P<extends>[\w.]+))?'
    r'(?:\s+implements\s+(?P<implements>[^{]+))?'
    r'\s*\{'
)
ES_IMPORT_RE = re.compile(
    r'import\s+(?:type\s+)?(?:\{(?P<named>[^}]*)\}|(?P<default>\w+))\s+from\s+'
    r'[\'"](?P<module>[^\'"]+)[\'"]'
)
CONSTRUCTOR_RE = re.compile(r'constructor\s*\(')
STRING_VALUE_RE = re.compile(r'''['"]([^'"]+)['"]''')

# Special ANGULAR_SUBPROJECTS entry (not a real folder under projects/) that
# means "also index the workspace's own root app shell" — i.e. ANGULAR_PROJECT's
# own src/ (main.ts, app module, etc.), which normally sits outside projects/
# and would otherwise never get indexed at all.
ROOT_APP_SENTINEL = "ROOT_APP"

DECORATOR_TO_NODE_TYPE = {
    "Component": "NG_COMPONENT",
    "NgModule": "NG_MODULE",
    "Injectable": "NG_SERVICE",
    "Directive": "NG_DIRECTIVE",
    "Pipe": "NG_PIPE",
}

JS_LITERAL_WORDS = {"true", "false", "null", "undefined", "this"}


def _extract_array(args_text, key):
    m = re.search(rf'\b{key}\s*:\s*\[', args_text)
    if not m:
        return []
    open_idx = args_text.index("[", m.end() - 1)
    close_idx = find_matching(args_text, open_idx, "[", "]")
    inner = args_text[open_idx + 1: close_idx - 1]
    return sorted(set(
        t for t in re.findall(r'\b[A-Z]\w*\b', inner) if t not in JS_LITERAL_WORDS
    ))


def _extract_string_field(args_text, key):
    m = re.search(rf'\b{key}\s*:\s*(\[?)\s*([\'"][^\'"]*[\'"])', args_text)
    if not m:
        return None
    sm = STRING_VALUE_RE.search(m.group(2))
    return sm.group(1) if sm else None


def _parse_constructor_deps(body_text):
    cm = CONSTRUCTOR_RE.search(body_text)
    if not cm:
        return []
    open_idx = cm.end() - 1
    close_idx = find_matching(body_text, open_idx, "(", ")")
    params_text = body_text[open_idx + 1: close_idx - 1]
    deps = []
    for param in split_top_level(params_text):
        cleaned = re.sub(r'@\w+\s*\((?:[^()]|\([^()]*\))*\)', '', param).strip()
        cleaned = re.sub(r'^(private|public|protected|readonly)\s+', '', cleaned).strip()
        if ":" not in cleaned:
            continue
        pname, _, ptype = cleaned.rpartition(":")
        ptype = strip_type(ptype.strip().rstrip("?"))
        pname = pname.strip().lstrip("?")
        if ptype and pname:
            deps.append((pname, ptype))
    return deps


def _parse_file(db, registry, project_name, subproject_name, subproject_path,
                 file_path, sub_node_id, stats):
    text = read_text(file_path)
    if text is None:
        stats["errors"] += 1
        return

    rel_file = os.path.relpath(file_path, subproject_path)
    file_qname = f"ng:{project_name}:{subproject_name}:{rel_file}"
    file_node_id = db.add_node(
        "FILE", os.path.basename(file_path), qualified_name=file_qname,
        service=subproject_name, project_type="angular", file_path=rel_file,
    )
    db.add_edge(sub_node_id, "CONTAINS", target_id=file_node_id, service=subproject_name)
    stats["files"] += 1

    for m in ES_IMPORT_RE.finditer(text):
        module = m.group("module")
        if not module.startswith("."):
            continue  # skip node_modules/angular core imports, keep local ones
        names = [n.strip().split(" as ")[0].strip() for n in m.group("named").split(",")] \
            if m.group("named") else [m.group("default")]
        for n in names:
            if n:
                db.add_edge(file_node_id, "IMPORTS", target_name=n,
                            service=subproject_name, metadata={"module": module})

    for dm in DECORATOR_OPEN_RE.finditer(text):
        try:
            _parse_decorated_class(db, registry, subproject_name, project_name,
                                    rel_file, text, dm, file_node_id, stats)
        except Exception:
            stats["errors"] += 1


def _parse_decorated_class(db, registry, subproject_name, project_name,
                            rel_file, text, dm, file_node_id, stats):
    decorator = dm.group(1)
    open_idx = dm.end() - 1
    close_idx = find_matching(text, open_idx, "(", ")")
    args_text = text[open_idx + 1: close_idx - 1]

    after = text[close_idx:]
    cm = CLASS_HEADER_RE.match(after)
    if not cm:
        return
    name = cm.group("name")
    body_start_in_after = cm.end() - 1
    body_start = close_idx + body_start_in_after
    body_end = find_matching(text, body_start, "{", "}")
    body_text = text[body_start:body_end]

    start_line = line_of(text, dm.start())
    end_line = line_of(text, body_end)
    node_type = DECORATOR_TO_NODE_TYPE[decorator]
    qname = f"ng:{project_name}:{subproject_name}:{rel_file}:{name}"

    metadata = {"decorator": decorator}
    if decorator == "Component":
        metadata["selector"] = _extract_string_field(args_text, "selector")
    elif decorator == "Directive":
        metadata["selector"] = _extract_string_field(args_text, "selector")
    elif decorator == "Pipe":
        metadata["pipe_name"] = _extract_string_field(args_text, "name")
    elif decorator == "Injectable":
        metadata["provided_in"] = _extract_string_field(args_text, "providedIn")

    class_node_id = db.add_node(
        node_type, name, qualified_name=qname, service=subproject_name,
        project_type="angular", file_path=rel_file, start_line=start_line,
        end_line=end_line, metadata=metadata,
    )
    registry.register(name, qname)
    db.add_edge(file_node_id, "DECLARES", target_id=class_node_id, service=subproject_name)
    stats["classes"] += 1
    stats.setdefault(node_type, 0)
    stats[node_type] += 1

    if cm.group("extends"):
        db.add_edge(class_node_id, "EXTENDS", target_name=strip_type(cm.group("extends")),
                    service=subproject_name)

    if decorator == "NgModule":
        for target in _extract_array(args_text, "declarations"):
            db.add_edge(class_node_id, "DECLARES", target_name=target, service=subproject_name)
        for target in _extract_array(args_text, "imports"):
            db.add_edge(class_node_id, "IMPORTS", target_name=target, service=subproject_name)
        for target in _extract_array(args_text, "exports"):
            db.add_edge(class_node_id, "EXPORTS", target_name=target, service=subproject_name)

    for pname, ptype in _parse_constructor_deps(body_text):
        if ptype in ("String", "number", "boolean", "any", "void", "object"):
            continue
        db.add_edge(class_node_id, "DEPENDS_ON", target_name=ptype,
                    service=subproject_name, metadata={"param_name": pname})


def parse_project(db, project_name, project_root):
    return db.add_node(
        "SERVICE", project_name, qualified_name=f"ng:{project_name}",
        project_type="angular", file_path=project_root,
    )


def parse_subproject(db, registry: NameRegistry, project_name, project_node_id,
                      subproject_name, subproject_path, progress=None):
    stats = {"files": 0, "classes": 0, "errors": 0}

    sub_node_id = db.add_node(
        "NG_SUBPROJECT", subproject_name,
        qualified_name=f"ng:{project_name}:{subproject_name}",
        service=subproject_name, project_type="angular", file_path=subproject_path,
    )
    db.add_edge(project_node_id, "CONTAINS", target_id=sub_node_id, service=project_name)

    src_dir = os.path.join(subproject_path, "src")
    walk_root = src_dir if os.path.isdir(src_dir) else subproject_path

    for file_path in walk_source_files(walk_root, (".ts",), ignored_suffixes=(".spec.ts", ".d.ts")):
        _parse_file(db, registry, project_name, subproject_name, subproject_path,
                    file_path, sub_node_id, stats)
        if progress:
            progress(subproject_name, file_path, stats)

    return stats
