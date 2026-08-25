"""Regex-based Java parser: classes/interfaces/enums, methods, REST
endpoints (@*Mapping), and dependency edges (extends/implements/@Autowired).
"""
import os
import re

from .common import (
    NameRegistry, line_of, read_text, strip_type, walk_source_files,
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


def _parse_annotations(annotation_blob):
    return [
        {"name": m.group(1).rsplit(".", 1)[-1], "args": m.group(2) or ""}
        for m in ANNOTATION_RE.finditer(annotation_blob or "")
    ]


def _annotation_path(args):
    if not args:
        return None
    m = QUOTED_STRING_RE.search(args)
    return m.group(1) if m else None


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


def _parse_file(db, registry, service_name, service_path, file_path, service_node_id, stats):
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
                         package, file_node_id, stats)
        except Exception:
            stats["errors"] += 1


def _parse_class(db, registry, service_name, rel_file, text, cm, package,
                  file_node_id, stats):
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
            base_path = _annotation_path(a["args"]) or ""

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
                       class_node_id, qname, base_path, stats)


def _parse_method(db, service_name, rel_file, full_text, body_start, mm,
                   class_node_id, class_qname, base_path, stats):
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
            sub_path = _annotation_path(a["args"]) or ""
        elif a["name"] == "RequestMapping":
            rm = REQUEST_METHOD_RE.search(a["args"] or "")
            http_method = rm.group(1) if rm else "REQUEST"
            sub_path = _annotation_path(a["args"]) or ""

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


def parse_service(db, registry: NameRegistry, service_name, service_path, progress=None):
    stats = {"files": 0, "classes": 0, "methods": 0, "endpoints": 0, "errors": 0}

    service_node_id = db.add_node(
        "SERVICE", service_name, qualified_name=f"java:{service_name}",
        service=service_name, project_type="java", file_path=service_path,
    )

    for file_path in walk_source_files(service_path, (".java",), ignored_suffixes=("Test.java", "Tests.java")):
        _parse_file(db, registry, service_name, service_path, file_path,
                    service_node_id, stats)
        if progress:
            progress(service_name, file_path, stats)

    return stats
