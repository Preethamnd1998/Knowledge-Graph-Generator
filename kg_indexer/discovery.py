"""Auto-discovery of backend services and UI projects.

Point BACKEND_ROOT / UI_ROOT (kg-config.env) at a folder and every project
inside it is found from marker files alone — nothing is scanned or parsed here.

Backend  : a child folder with a pom.xml is a Java service.
UI       : a child folder is one of
  * Angular — has angular.json. Its indexable parts are the root app (src/, the
              "shell"; absent in a library-only repo) plus every non-empty
              folder under projects/. A repo with no projects/ is just the root
              app; the monolith (lifetrenz-core) is the same shape with many
              projects/. angular.json's own project list is NOT used: split
              repos keep a copy of the monolith's 53-entry list while only a
              few of those folders exist.
  * React   — no angular.json, package.json depends on react.
Anything else is reported in `Plan.skipped` with the reason, never silently
dropped (a Python/Node service has no parser here; Code Cortex covers those).
"""
import json
import os
from dataclasses import dataclass, field

from .parsers.angular_parser import ROOT_APP_SENTINEL


@dataclass
class AngularTarget:
    project: str      # path relative to ROOT_PATH, e.g. "WAPP/adt"
    subprojects: list  # ROOT_APP_SENTINEL and/or folder names under projects/


@dataclass
class Plan:
    backend: list = field(default_factory=list)   # relative paths
    angular: list = field(default_factory=list)   # AngularTarget
    react: list = field(default_factory=list)     # absolute paths
    skipped: list = field(default_factory=list)   # (relative path, reason)


def split_list(value):
    return [p.strip() for p in (value or "").replace(";", ",").split(",") if p.strip()]


def _children(abs_dir):
    try:
        names = sorted(os.listdir(abs_dir))
    except OSError:
        return []
    return [n for n in names if not n.startswith(".") and os.path.isdir(os.path.join(abs_dir, n))]


def _non_empty(abs_dir):
    try:
        return any(not n.startswith(".") for n in os.listdir(abs_dir))
    except OSError:
        return False


def _package_deps(abs_dir):
    try:
        with open(os.path.join(abs_dir, "package.json"), "r", encoding="utf-8") as fh:
            pkg = json.load(fh)
    except (OSError, ValueError):
        return None
    return {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}


def _why_not_indexed(abs_dir):
    if os.path.isfile(os.path.join(abs_dir, "package.json")):
        return "Node project without react or angular.json — no parser"
    if any(os.path.isfile(os.path.join(abs_dir, f)) for f in ("requirements.txt", "pyproject.toml", "setup.py")):
        return "Python project — no Knowledge Graph parser (Code Cortex indexes it)"
    if not _non_empty(abs_dir):
        return "empty folder"
    return "no pom.xml / angular.json / react package.json at its root"


def classify_ui(abs_dir):
    """('angular', [subprojects]) | ('react', None) | (None, reason)."""
    if os.path.isfile(os.path.join(abs_dir, "angular.json")):
        subs = []
        src = os.path.join(abs_dir, "src")
        if os.path.isdir(src) and _non_empty(src):
            subs.append(ROOT_APP_SENTINEL)
        projects = os.path.join(abs_dir, "projects")
        subs += [n for n in _children(projects) if _non_empty(os.path.join(projects, n))]
        if subs:
            return "angular", subs
        return None, "angular.json but neither src/ nor a non-empty projects/ folder"
    deps = _package_deps(abs_dir)
    if deps is not None and "react" in deps:
        return "react", None
    return None, _why_not_indexed(abs_dir)


def discover(root_path, backend_roots=(), ui_roots=(), exclude=(), plan=None):
    """Scan the given roots (relative to root_path, or absolute) into a Plan.
    Entries already in `plan` (explicit config) are kept and not repeated."""
    plan = plan or Plan()
    excluded = {e.lower() for e in exclude}
    have_backend = set(plan.backend)
    have_angular = {t.project for t in plan.angular}
    have_react = {os.path.normpath(p) for p in plan.react}

    def rel(root, name):
        # keep the user's own spelling of the root ("WAPP"), so service names
        # stay "WAPP/adt" and never turn into an absolute path
        return f"{root.rstrip('/' + os.sep)}/{name}"

    for root in backend_roots:
        abs_root = root if os.path.isabs(root) else os.path.join(root_path, root)
        if not os.path.isdir(abs_root):
            plan.skipped.append((root, "BACKEND_ROOT not found on disk"))
            continue
        for name in _children(abs_root):
            r = rel(root, name)
            if name.lower() in excluded or r in have_backend:
                continue
            child = os.path.join(abs_root, name)
            if os.path.isfile(os.path.join(child, "pom.xml")):
                plan.backend.append(r)
            else:
                plan.skipped.append((r, _why_not_indexed(child)))

    for root in ui_roots:
        abs_root = root if os.path.isabs(root) else os.path.join(root_path, root)
        if not os.path.isdir(abs_root):
            plan.skipped.append((root, "UI_ROOT not found on disk"))
            continue
        for name in _children(abs_root):
            r = rel(root, name)
            if name.lower() in excluded:
                continue
            child = os.path.join(abs_root, name)
            kind, detail = classify_ui(child)
            if kind == "angular":
                if r not in have_angular:
                    plan.angular.append(AngularTarget(r, detail))
            elif kind == "react":
                if os.path.normpath(child) not in have_react:
                    plan.react.append(child)
            else:
                plan.skipped.append((r, detail))
    return plan
