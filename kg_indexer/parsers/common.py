"""Shared helpers used by the Java / Angular / React parsers.

All parsing here is regex/text based on purpose — no javalang / tree-sitter
/ TS compiler dependency is required, so the tool has zero third-party
dependencies and works anywhere Python 3.8+ runs. This trades perfect AST
accuracy for zero install friction; see README.md "Known limitations".
"""
import os
import re

IGNORED_DIR_NAMES = {
    ".git", ".idea", ".vscode", "node_modules", "target", "build", "bin",
    "dist", "out", "coverage", ".angular", "__pycache__", ".mvn",
}


def walk_source_files(root_dir, extensions, ignored_suffixes=()):
    """Yield absolute file paths under root_dir matching one of `extensions`
    (e.g. (".java",)), skipping build/vendor directories and any file whose
    name ends with one of `ignored_suffixes` (e.g. ".spec.ts", "Test.java").
    """
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIR_NAMES]
        for filename in filenames:
            if not filename.endswith(extensions):
                continue
            if any(filename.endswith(suf) for suf in ignored_suffixes):
                continue
            yield os.path.join(dirpath, filename)


def read_text(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def line_of(text, index):
    """1-based line number of a character offset within `text`."""
    return text.count("\n", 0, index) + 1


def strip_type(raw_type):
    """Reduce a Java/TS type expression to its base identifier: strips
    generics ("List<Foo>" -> "List"), arrays ("Foo[]" -> "Foo"), and
    leading access modifiers, keeping only the final segment of a dotted
    path ("com.acme.Foo" -> "Foo").
    """
    if raw_type is None:
        return None
    t = raw_type.strip()
    t = t.split("<", 1)[0]
    t = t.replace("[]", "")
    t = t.strip().rstrip("?")
    if "." in t:
        t = t.rsplit(".", 1)[-1]
    return t.strip() or None


def find_matching(text, open_index, open_ch="(", close_ch=")"):
    """Return the index just after the close_ch matching the open_ch at
    open_index, skipping over string/template/regex-unaware literals well
    enough for TS/JS object & argument lists (handles nested (), {}, [] of
    the same pair, plus quoted strings so a bracket inside a string doesn't
    throw off the count).
    """
    depth = 0
    i = open_index
    n = len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def split_top_level(s, sep=","):
    """Split on `sep` only at bracket depth 0 across (), {}, [], <>, and
    quoted strings — for parameter/argument lists that may themselves
    contain generics, decorators, or nested literals.
    """
    if not s:
        return []
    parts, depth, current, quote = [], 0, [], None
    openers, closers = "([{<", ")]}>"
    for ch in s:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            current.append(ch)
            continue
        if ch in openers:
            depth += 1
        elif ch in closers:
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


class NameRegistry:
    """Maps a short/simple name to the list of qualified names that could
    match it, built up as nodes are discovered during parsing. Used to
    resolve edges (extends, implements, @Autowired field type, constructor
    injection, ...) once the full graph is loaded.
    """

    def __init__(self):
        self._map = {}

    def register(self, simple_name, qualified_name):
        self._map.setdefault(simple_name, [])
        if qualified_name not in self._map[simple_name]:
            self._map[simple_name].append(qualified_name)

    def get(self, simple_name):
        return self._map.get(simple_name)

    def as_dict(self):
        return self._map


# --- UI "API constant" support (Angular/React) -----------------------------
#
# Shared by angular_parser.py and react_parser.py so both languages detect
# `export class FooApiConstant(s) { public static X = "path/segments"; }`
# identically (see kg_indexer/db.py GraphDB.resolve_api_constant_edges for
# how the extracted path is matched against a backend ENDPOINT node).

_API_CONSTANT_SUFFIXES = ("apiconstant", "apiconstants")


def is_api_constant_class_name(name):
    lname = name.lower()
    return lname.endswith(_API_CONSTANT_SUFFIXES)


# Any top-level TS class (no decorator required — these are plain constant
# holders, not @Component/@Injectable), captured generically so the caller
# can filter by is_api_constant_class_name() before doing anything with it.
PLAIN_CLASS_RE = re.compile(
    r'(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>\w+)'
    r'(?:\s+extends\s+[\w.]+)?(?:\s+implements\s+[^{]+)?\s*\{'
)

# `public static FIELD = "value";` / `private static FIELD: string = 'value';`
# — value must be a plain quoted string literal (no concatenation) to be
# treated as a resolvable API identity; see AGENTS.md for why concatenated
# definitions are intentionally out of scope (none exist in the real
# workspace as of writing this).
STATIC_STRING_FIELD_RE = re.compile(
    r'(?:public\s+|private\s+|protected\s+)?static\s+(?:readonly\s+)?'
    r'(?P<field>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:\s*:\s*\w+)?\s*=\s*'
    r'(?:"(?P<dq>[^"]*)"|\'(?P<sq>[^\']*)\')\s*;'
)

# `SomeApiConstant.SOME_FIELD` referenced anywhere in a .ts/.tsx file — this
# is how a component/service *uses* an API constant, regardless of whether
# its defining class lives in this file, another file, or another
# sub-project/library (cross-file resolution happens the same way every
# other target_name-only edge in this codebase resolves — see
# GraphDB.resolve_pending_edges).
API_CONSTANT_USAGE_RE = re.compile(
    r'\b(?P<cls>[A-Za-z_]\w*(?:ApiConstants?|APIConstants?))\.(?P<field>[A-Z][A-Z0-9_]*)\b'
)


def normalize_ui_path(raw):
    """Strip a query string and leading/trailing slashes from a UI-side API
    constant's literal value, returning (normalized_path, segments). Never
    guesses at a runtime-appended suffix (e.g. `+ "/" + id`) — only the
    literal, statically-known portion is normalized."""
    if raw is None:
        return None, []
    value = raw.split("?", 1)[0].strip().strip("/")
    if not value:
        return "", []
    segments = [s for s in value.split("/") if s]
    return "/".join(segments), segments


def path_segments_match(a_segments, b_segments):
    """True if two '/'-split paths match, treating a `{...}` segment on
    EITHER side as a wildcard (path-variable) that matches anything at that
    position — used to compare a UI API constant's path against a Java
    @*Mapping path, or a Feign/WebClient/RestTemplate call's path against
    the controller endpoint it's meant to reach. Segment *counts* must match
    exactly — no guessing at a variable-length wildcard."""
    if len(a_segments) != len(b_segments):
        return False
    for a, b in zip(a_segments, b_segments):
        if a == b:
            continue
        a_is_var = a.startswith("{") and a.endswith("}")
        b_is_var = b.startswith("{") and b.endswith("}")
        if not (a_is_var or b_is_var):
            return False
    return True


def path_specificity(a_segments, b_segments):
    """Count of segments that matched as an exact literal (neither side a
    `{...}` wildcard) between two already-matching paths (see
    path_segments_match). Used to prefer a more specific route when a UI
    path structurally matches more than one endpoint — e.g. "core/patient/
    list" structurally matches both a literal "/patient/list" mapping and a
    "/patient/{id}" mapping (the wildcard side matches any literal). Spring's
    own routing always prefers the all-literal match; ties are broken the
    same way here rather than reporting a false ambiguity between a real
    match and a coincidental wildcard one."""
    return sum(1 for a, b in zip(a_segments, b_segments) if a == b)
