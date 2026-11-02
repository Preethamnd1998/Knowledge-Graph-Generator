"""Regex-based Angular parser: @Component / @NgModule / @Injectable /
@Directive / @Pipe classes, module declarations/imports/exports,
constructor-injected dependencies, local ES-module imports, template
event bindings ((click)="handler()" and friends), and Angular route
configuration (path/component/canActivate*/canMatch/canLoad/resolve/
children) — see _parse_html_file and _parse_routes_in_text.
"""
import os
import re

from .common import (
    API_CONSTANT_USAGE_RE, NameRegistry, PLAIN_CLASS_RE, STATIC_STRING_FIELD_RE,
    emit_suffix_constant_usages, find_matching, is_api_constant_class_name, is_constant_class_name, line_of, looks_like_api_path, normalize_ui_path,
    read_text, split_top_level, strip_type, walk_source_files,
)

DECORATOR_OPEN_RE = re.compile(r'@(Component|NgModule|Injectable|Directive|Pipe)\s*\(')
COMPONENT_DECORATOR_RE = re.compile(r'@Component\s*\(')
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

# --- Template events -------------------------------------------------------
#
# `(eventName)="expr"` — any parenthesized identifier (dots allowed for
# modifier syntax like `(keydown.enter)`), never `[binding]`, `{{interp}}`,
# or `[attr.x]`/`[(banana)]` (those don't start with a bare `(name)` at a
# tag-attribute position and so never match this pattern in practice — a
# genuine two-way `[(ngModel)]` binding starts with `[(`, which this regex
# requires NOT to be preceded by `[`, enforced via the negative lookbehind).
TEMPLATE_EVENT_RE = re.compile(
    r'(?<!\[)\(([a-zA-Z][\w.]*)\)\s*=\s*"([^"]*)"'
)
# `savePatient()`, `savePatient(patient)`, `onSave($event)` — a bare call,
# optionally with arguments we don't need to parse (we only need the
# handler's own name to look it up).
SELF_CALL_RE = re.compile(r'^([a-zA-Z_]\w*)\s*\(')
# `patientService.savePatient()` — one level of member access then a call.
# Anything deeper (`a.b.c()`) or non-call (`a.b`) is deliberately left
# UNRESOLVED rather than guessed at.
MEMBER_CALL_RE = re.compile(r'^([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(')

# --- Angular routes ---------------------------------------------------------
#
# Either `const routes: Routes = [...]` (any identifier ending in "outes",
# case-insensitive on the "R"/"r" so both `routes` and `appRoutes` match) or
# an inline array literal passed straight to forRoot/forChild — both forms
# are real, both seen in this codebase (app-routing.module.ts uses the
# former via a separate const, home-routing.module.ts inlines the latter).
ROUTES_CONST_RE = re.compile(r'\b\w*[Rr]outes\w*\s*:\s*Routes\s*=\s*\[')
ROUTER_MODULE_CALL_RE = re.compile(r'RouterModule\.(?:forRoot|forChild)\(\s*\[')
GUARD_KEYS = ("canActivate", "canActivateChild", "canDeactivate", "canMatch", "canLoad")

# `loadChildren: () => import('./x/y.module').then((m) => m.YModule)` — the
# two real forms seen in this codebase: parenthesized or bare single-arg
# callback (`(m) => m.X` / `m => m.X`). Captures (import_path, module_symbol).
# Resolving import_path (which may be a TS path alias like "@inpatient/...")
# to an actual file on disk is out of scope (module-compiler territory) —
# only the raw import path + symbol are captured, never guessed further.
LAZY_ROUTE_RE = re.compile(
    r"loadChildren\s*:\s*\(\s*\)\s*=>\s*import\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"\s*\.then\(\s*\(?\s*\w+\s*\)?\s*=>\s*\w+\.(\w+)\s*,?\s*\)"
)


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


def _extract_identifier_field(args_text, key):
    """`component: PatientComponent` — an unquoted identifier value (as
    opposed to _extract_string_field's quoted-string value)."""
    m = re.search(rf'\b{key}\s*:\s*([A-Za-z_]\w*)', args_text)
    return m.group(1) if m else None


def _extract_object_map(args_text, key):
    """`resolve: { patient: PatientResolver, doctor: DoctorResolver }` ->
    {"patient": "PatientResolver", "doctor": "DoctorResolver"}. Only
    identifier values are captured (a string/function-literal resolver
    value is left out rather than guessed at)."""
    m = re.search(rf'\b{key}\s*:\s*\{{', args_text)
    if not m:
        return {}
    open_idx = args_text.index("{", m.end() - 1)
    close_idx = find_matching(args_text, open_idx, "{", "}")
    inner = args_text[open_idx + 1: close_idx - 1]
    result = {}
    for entry in split_top_level(inner):
        if ":" not in entry:
            continue
        k, _, v = entry.partition(":")
        k = k.strip().strip("'\"")
        vm = re.match(r'\s*([A-Za-z_]\w*)', v)
        if k and vm:
            result[k] = vm.group(1)
    return result


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

    for pm in PLAIN_CLASS_RE.finditer(text):
        strict = is_api_constant_class_name(pm.group("name"))
        if not strict and not is_constant_class_name(pm.group("name")):
            continue
        try:
            _parse_api_constant_class(db, registry, project_name, subproject_name,
                                       rel_file, text, pm, file_node_id, stats, only_url_like=not strict)
        except Exception:
            stats["errors"] += 1

    for um in API_CONSTANT_USAGE_RE.finditer(text):
        key = f"{um.group('cls')}.{um.group('field')}"
        db.add_edge(file_node_id, "USES_API_CONSTANT", target_name=key, service=subproject_name)
    stats["API_CONSTANT"] = stats.get("API_CONSTANT", 0) + emit_suffix_constant_usages(
        db, "angular", project_name, subproject_name, rel_file, text, file_node_id)

    try:
        _parse_routes_in_text(db, registry, project_name, subproject_name, rel_file,
                               text, file_node_id, stats)
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


def _parse_api_constant_class(db, registry, project_name, subproject_name, rel_file,
                               text, pm, file_node_id, stats, only_url_like=False):
    """`export class FooApiConstant { public static X = "path/segments"; }`
    — one API_CONSTANT node per field, not one node per class: the field is
    the actual "API identity" a UI call site references (`FooApiConstant.X`),
    see common.py's module docs. Node identity includes rel_file (same
    reason as every other Angular node — this class name repeats across
    feature libraries, sometimes with the *same* field name resolving to
    the *same* backend path, which is a legitimate duplicate, not a bug)."""
    class_name = pm.group("name")
    body_start = pm.end() - 1
    body_end = find_matching(text, body_start, "{", "}")
    body_text = text[body_start:body_end]

    count = 0
    for fm in STATIC_STRING_FIELD_RE.finditer(body_text):
        field_name = fm.group("field")
        raw_value = fm.group("dq") if fm.group("dq") is not None else fm.group("sq")
        if only_url_like and not looks_like_api_path(raw_value):
            continue   # a generic *Constants class: keep only the fields that are endpoint paths
        normalized_path, segments = normalize_ui_path(raw_value)
        qname = f"ng:{project_name}:{subproject_name}:{rel_file}:{class_name}.{field_name}"
        field_line = line_of(text, body_start + fm.start())
        node_id = db.add_node(
            "API_CONSTANT", f"{class_name}.{field_name}", qualified_name=qname,
            service=subproject_name, project_type="angular", file_path=rel_file,
            start_line=field_line,
            metadata={
                "class_name": class_name, "field_name": field_name, "raw_value": raw_value,
                "normalized_path": normalized_path, "segments": segments,
            },
        )
        registry.register(f"{class_name}.{field_name}", qname)
        db.add_edge(file_node_id, "DECLARES", target_id=node_id, service=subproject_name)
        count += 1
    stats["API_CONSTANT"] = stats.get("API_CONSTANT", 0) + count


# --- Angular routes ---------------------------------------------------------

def _iter_top_level_objects(text, start, end):
    """Yield (start, end) absolute-offset spans of every `{...}` object
    literal that begins at bracket depth 0 within text[start:end] (e.g.
    each element of a route array). `end` of each span is exclusive, one
    past the matching `}`. Operates on absolute offsets into the SAME
    string passed by the caller so line numbers stay accurate however
    deep the recursion (nested `children` arrays) goes."""
    i = start
    quote = None
    while i < end:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "{":
            obj_end = find_matching(text, i, "{", "}")
            yield i, obj_end
            i = obj_end
            continue
        i += 1


def _parse_route_object(db, registry, project_name, subproject_name, rel_file,
                         text, obj_start, obj_end, parent_route_id, counter, file_node_id, stats):
    inner_start, inner_end = obj_start + 1, obj_end - 1
    inner = text[inner_start:inner_end]

    path = _extract_string_field(inner, "path")
    if path is None:
        path = ""
    component_name = _extract_identifier_field(inner, "component")
    is_lazy = "loadChildren" in inner
    lazy_module_path = None
    lazy_module_symbol = None
    if is_lazy:
        lm = LAZY_ROUTE_RE.search(inner)
        if lm:
            lazy_module_path, lazy_module_symbol = lm.group(1), lm.group(2)
        # else: loadChildren present but not in either recognized form ->
        # is_lazy stays true (a real fact: this route has no component,
        # never fabricate ROUTES_TO for it) while lazy_module_path/symbol
        # stay None (UNRESOLVED — never guessed from an unrecognized shape).
    guards = {}
    for key in GUARD_KEYS:
        names = _extract_array(inner, key)
        if names:
            guards[key] = names
    resolvers = _extract_object_map(inner, "resolve")

    start_line = line_of(text, obj_start)
    counter[0] += 1
    qname = f"ng:{project_name}:{subproject_name}:{rel_file}:route:{counter[0]}"
    metadata = {
        "path": path, "component_name": component_name, "lazy": is_lazy,
        "lazy_module_path": lazy_module_path, "lazy_module_symbol": lazy_module_symbol,
        "guards": guards, "resolvers": resolvers,
    }
    route_id = db.add_node(
        "NG_ROUTE", path or "(empty)", qualified_name=qname, service=subproject_name,
        project_type="angular", file_path=rel_file, start_line=start_line, metadata=metadata,
    )
    db.add_edge(file_node_id, "DECLARES", target_id=route_id, service=subproject_name)
    stats["routes"] = stats.get("routes", 0) + 1

    if component_name:
        db.add_edge(route_id, "ROUTES_TO", target_name=component_name, service=subproject_name)
    for guard_type, names in guards.items():
        for gname in names:
            db.add_edge(route_id, "GUARDED_BY", target_name=gname, service=subproject_name,
                        metadata={"guard_type": guard_type})
    for key, resolver_name in resolvers.items():
        db.add_edge(route_id, "RESOLVED_BY", target_name=resolver_name, service=subproject_name,
                    metadata={"resolver_key": key})
    if parent_route_id is not None:
        db.add_edge(parent_route_id, "CONTAINS_ROUTE", target_id=route_id, service=subproject_name)

    children_m = re.search(r'\bchildren\s*:\s*\[', inner)
    if children_m:
        abs_open = inner_start + inner.index("[", children_m.end() - 1)
        abs_close = find_matching(text, abs_open, "[", "]")
        for cstart, cend in _iter_top_level_objects(text, abs_open + 1, abs_close - 1):
            _parse_route_object(db, registry, project_name, subproject_name, rel_file,
                                 text, cstart, cend, route_id, counter, file_node_id, stats)
    return route_id


def _parse_routes_in_text(db, registry, project_name, subproject_name, rel_file,
                           text, file_node_id, stats):
    """Finds the file's top-level Angular route array — either a separate
    `const xRoutes: Routes = [...]` or an array literal passed straight to
    RouterModule.forRoot/forChild(...) — and walks every route object in
    it (recursing into `children`). A file with neither pattern (the
    overwhelming majority of .ts files) costs two cheap regex probes and
    nothing else."""
    m = ROUTES_CONST_RE.search(text)
    if not m:
        m = ROUTER_MODULE_CALL_RE.search(text)
    if not m:
        return
    open_idx = text.index("[", m.end() - 1)
    close_idx = find_matching(text, open_idx, "[", "]")
    counter = [0]
    for ostart, oend in _iter_top_level_objects(text, open_idx + 1, close_idx - 1):
        _parse_route_object(db, registry, project_name, subproject_name, rel_file,
                            text, ostart, oend, None, counter, file_node_id, stats)


# --- Template events ---------------------------------------------------------

def _resolve_owning_component(ts_path):
    """Reads the sibling .ts file for the (expected single) @Component
    class it declares. Returns (class_name, constructor_deps_by_param_name)
    or (None, {}) if no @Component class is found there — never guessed
    from the file name alone, only from the real decorator + class header."""
    text = read_text(ts_path)
    if text is None:
        return None, {}
    dm = COMPONENT_DECORATOR_RE.search(text)
    if not dm:
        return None, {}
    open_idx = dm.end() - 1
    close_idx = find_matching(text, open_idx, "(", ")")
    after = text[close_idx:]
    cm = CLASS_HEADER_RE.match(after)
    if not cm:
        return None, {}
    body_start_in_after = cm.end() - 1
    body_start = close_idx + body_start_in_after
    body_end = find_matching(text, body_start, "{", "}")
    body_text = text[body_start:body_end]
    deps = dict(_parse_constructor_deps(body_text))
    return cm.group("name"), deps


def _classify_handler(expr):
    """Classifies the FIRST ';'-separated statement of a template event's
    handler expression (a real multi-statement handler like
    `updateRead(x); $event.stopPropagation()` still gets one deterministic
    primary target — the full raw expression is kept in metadata either
    way). Returns (handler_kind, method_name, member_name):
      - "self_call":  savePatient() / savePatient(arg) / onSave($event)
      - "member_call": patientService.savePatient() — one level of member
        access then a call; member_name is the injected field to resolve
        via the component's own constructor deps.
      - "unsupported": assignment (`x = 'y'`), bare member access with no
        call, or anything deeper than one level — never guessed.
    """
    first = expr.split(";")[0].strip()
    mm = MEMBER_CALL_RE.match(first)
    if mm:
        return "member_call", mm.group(2), mm.group(1)
    sm = SELF_CALL_RE.match(first)
    if sm:
        return "self_call", sm.group(1), None
    return "unsupported", None, None


def _parse_html_file(db, registry, project_name, subproject_name, subproject_path,
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
    stats["files"] = stats.get("files", 0) + 1
    stats["template_files"] = stats.get("template_files", 0) + 1

    # Angular CLI convention: foo.component.html sits beside foo.component.ts.
    # Verified against the real @Component decorator there, not assumed.
    ts_path = os.path.splitext(file_path)[0] + ".ts"
    component_name, deps = (None, {})
    if os.path.isfile(ts_path):
        component_name, deps = _resolve_owning_component(ts_path)

    count = 0
    for em in TEMPLATE_EVENT_RE.finditer(text):
        event_name = em.group(1)
        handler_expr = em.group(2).strip()
        if not handler_expr:
            continue
        handler_kind, method_name, member_name = _classify_handler(handler_expr)
        start_line = line_of(text, em.start())
        count += 1
        qname = f"ng:{project_name}:{subproject_name}:{rel_file}:{start_line}:{event_name}:{count}"
        metadata = {
            "event_name": event_name, "handler_expression": handler_expr,
            "handler_kind": handler_kind, "component_name": component_name,
            "multi_statement": ";" in handler_expr,
        }
        if handler_kind == "self_call":
            metadata["method_name"] = method_name
        elif handler_kind == "member_call":
            metadata["member_name"] = member_name
            metadata["member_type"] = deps.get(member_name)
            metadata["method_name"] = method_name

        event_id = db.add_node(
            "TEMPLATE_EVENT", f"({event_name})", qualified_name=qname,
            service=subproject_name, project_type="angular", file_path=rel_file,
            start_line=start_line, metadata=metadata,
        )
        db.add_edge(file_node_id, "DECLARES", target_id=event_id, service=subproject_name)
        if component_name:
            db.add_edge(event_id, "DECLARED_IN", target_name=component_name, service=subproject_name)

        if handler_kind == "self_call":
            db.add_edge(event_id, "HANDLES_EVENT", target_name=method_name, service=subproject_name,
                        metadata={"handler_kind": "self_call", "component_name": component_name,
                                  "method_name": method_name})
        elif handler_kind == "member_call" and deps.get(member_name):
            db.add_edge(event_id, "HANDLES_EVENT", target_name=deps[member_name], service=subproject_name,
                        metadata={"handler_kind": "member_call", "member_name": member_name,
                                  "method_name": method_name})
        stats["TEMPLATE_EVENT"] = stats.get("TEMPLATE_EVENT", 0) + 1
    stats["events"] = stats.get("events", 0) + count


def parse_project(db, project_name, project_root):
    return db.add_node(
        "SERVICE", project_name, qualified_name=f"ng:{project_name}",
        project_type="angular", file_path=project_root,
    )


def parse_subproject(db, registry: NameRegistry, project_name, project_node_id,
                      subproject_name, subproject_path, progress=None, only_files=None):
    stats = {"files": 0, "classes": 0, "errors": 0}

    sub_node_id = db.add_node(
        "NG_SUBPROJECT", subproject_name,
        qualified_name=f"ng:{project_name}:{subproject_name}",
        service=subproject_name, project_type="angular", file_path=subproject_path,
    )
    db.add_edge(project_node_id, "CONTAINS", target_id=sub_node_id, service=project_name)

    src_dir = os.path.join(subproject_path, "src")
    walk_root = src_dir if os.path.isdir(src_dir) else subproject_path

    for file_path in walk_source_files(walk_root, (".ts", ".html"), ignored_suffixes=(".spec.ts", ".d.ts")):
        if only_files is not None and file_path not in only_files:
            continue
        if file_path.endswith(".html"):
            _parse_html_file(db, registry, project_name, subproject_name, subproject_path,
                              file_path, sub_node_id, stats)
        else:
            _parse_file(db, registry, project_name, subproject_name, subproject_path,
                        file_path, sub_node_id, stats)
        if progress:
            progress(subproject_name, file_path, stats)

    return stats
