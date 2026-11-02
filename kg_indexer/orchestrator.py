import json
import os
import time

from .config import Config
from .db import GraphDB
from . import incremental
from .gateway_prefix import strict_mode as gateway_strict_mode
from .parsers.common import NameRegistry, walk_source_files
from .parsers import angular_parser, java_parser, react_parser, spring_flow_parser
from .parsers.angular_parser import ROOT_APP_SENTINEL

# Must mirror the walk_source_files(...) call inside each parser's own
# parse_service/parse_subproject/parse_project — see java_parser.py:538,
# angular_parser.py:253, react_parser.py:206. Change detection happens here,
# once per target, before either backend pass runs (see _java_only_files),
# so it needs the same file set the parser would otherwise discover itself.
_JAVA_EXTS, _JAVA_IGNORED = (".java",), ("Test.java", "Tests.java")
_ANGULAR_EXTS, _ANGULAR_IGNORED = (".ts", ".html"), (".spec.ts", ".d.ts")
_REACT_EXTS = (".jsx", ".tsx", ".js", ".ts")
_REACT_IGNORED = (".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx", ".d.ts")


_PRIMARY_ANGULAR_PROJECT = "lifetrenz-core"


def _print(msg):
    print(msg, flush=True)


def _progress(service_name, file_path, stats, every=25):
    if stats["files"] % every == 0:
        _print(f"    [{service_name}] {stats['files']} files scanned...")


def _db_path_for(output_dir, name):
    safe = name.replace(os.sep, "_").replace("/", "_")
    return os.path.join(output_dir, f"{safe}.db")


def run(config: Config):
    started = time.time()
    os.makedirs(config.output_dir, exist_ok=True)

    include_backend = config.index_mode in ("all", "selective", "backend_only")
    include_frontend = config.index_mode in ("all", "selective", "frontend_only")
    only_backend_category = config.index_mode == "backend_only"
    only_frontend_category = config.index_mode == "frontend_only"

    backend_services = config.backend_services.enabled if include_backend and not only_frontend_category else []
    angular_subprojects = config.angular_subprojects.enabled if include_frontend and not only_backend_category else []
    react_enabled = include_frontend and not only_backend_category and bool(config.react_project_path.strip())

    _print("=" * 64)
    _print("Knowledge Graph Indexer")
    _print("=" * 64)
    _print(f"Config file     : {config.source_path}")
    _print(f"Index mode      : {config.index_mode}")
    _print(f"Root path       : {config.root_path}")
    _print(f"Output dir      : {config.output_dir}")
    _print(f"Single database : {config.single_database}")
    _print("")
    _print(f"Backend services enabled  : {len(backend_services)} "
            f"(disabled: {len(config.backend_services.disabled)})")
    _print(f"Angular subprojects enabled: {len(angular_subprojects)} "
            f"(disabled: {len(config.angular_subprojects.disabled)})")
    _print(f"React project             : {'enabled' if react_enabled else 'skipped'}")
    _print("")

    if config.single_database:
        db_path = _db_path_for(config.output_dir, "knowledge_graph")
        db = GraphDB(db_path, incremental=config.incremental)
        mode_label = "incremental" if (config.incremental and not db.is_fresh) else "full"
        _print(f"Writing combined graph to: {db_path} ({mode_label} index)\n")
        summaries = _index_all(config, db, backend_services, angular_subprojects, react_enabled, shared=True)
        pruned = db.prune_dangling_constant_usages()
        if pruned:
            _print(f"Removed {pruned} unresolved usages of non-API constant classes")
        orphans = db.prune_orphan_edges()
        if orphans:
            _print(f"Removed {orphans} orphaned edges (target node was re-indexed away)")
        removed = db.dedupe_edges()
        if removed:
            _print(f"Removed {removed} duplicate edges (re-emitted by incremental runs)")
        db.close()
        _print_summary("knowledge_graph.db", db_path, summaries)
    else:
        summaries = _index_all(config, None, backend_services, angular_subprojects,
                                react_enabled, shared=False)

    elapsed = time.time() - started
    _print("")
    _print("=" * 64)
    _print(f"Done in {elapsed:.1f}s")
    _print("=" * 64)


def _seed_gateway_prefix_map(db, gateway_prefix_map):
    """Add the gateway prefixes of backend services ALREADY in the graph (this
    run's own discoveries win). Without this a UI-only run (a newly split UI
    repo) has an empty map, so every UI constant it parses is marked
    `unknown_gateway_prefix` and silently loses its link to the backend."""
    for name, metadata in db.conn.execute("SELECT name, metadata FROM nodes WHERE node_type = 'SERVICE'"):
        prefix = (json.loads(metadata or "{}").get("gateway_prefix") or "").lower()
        if prefix and prefix not in gateway_prefix_map:
            gateway_prefix_map[prefix] = name


def _seeded_registry(db, project_type):
    """A NameRegistry pre-populated from existing nodes so an incremental
    run's cross-reference resolution can still see symbols defined in
    unchanged files it never re-parses this run."""
    registry = NameRegistry()
    if db is not None:
        for simple_name, qualified_name in db.iter_name_qualified_pairs(project_type):
            registry.register(simple_name, qualified_name)
    return registry


def _index_all(config, shared_db, backend_services, angular_subprojects, react_enabled, shared):
    seed_db = shared_db if (shared and config.incremental and not shared_db.is_fresh) else None
    java_registry = _seeded_registry(seed_db, "java")
    ng_registry = _seeded_registry(seed_db, "angular")
    react_registry = _seeded_registry(seed_db, "react")
    per_target_summaries = []
    # {registered spring.application.name -> our service_name} — the API
    # Gateway prefix map used to resolve UI API_CONSTANT paths and backend
    # Feign/WebClient/RestTemplate calls to a real ENDPOINT (see
    # java_parser._extract_gateway_prefix and db.py's resolve_* methods).
    # Read from each service's own config, never hand-mapped here.
    gateway_prefix_map = {}
    gateway_notes = []     # informational (explicit override used)
    gateway_findings = []  # unresolved / override / conflict / duplicate notes, printed + strict-checked below

    for service_name in backend_services:
        service_path = os.path.join(config.root_path, service_name)
        if not os.path.isdir(service_path):
            _print(f"[skip] backend service not found on disk: {service_path}")
            continue
        db = shared_db if shared else GraphDB(_db_path_for(config.output_dir, service_name), incremental=config.incremental)

        only_files = None
        if config.incremental and not db.is_fresh:
            all_java = list(walk_source_files(service_path, _JAVA_EXTS, ignored_suffixes=_JAVA_IGNORED))
            changed, unchanged = incremental.classify_files(db, service_name, service_path, all_java)
            removed = incremental.sweep_deleted(db, service_name, service_path, all_java)
            only_files = set(changed)
            _print(f"[java] indexing {service_name} (incremental: {len(changed)} changed, "
                   f"{len(unchanged)} unchanged, {removed} removed) ...")
        else:
            _print(f"[java] indexing {service_name} (full) ...")

        stats = java_parser.parse_service(db, java_registry, service_name, service_path,
                                           progress=_progress, only_files=only_files)
        flow_stats = spring_flow_parser.parse_service(db, java_registry, service_name, service_path,
                                                       only_files=only_files)
        gateway_prefix = stats.get("gateway_prefix")
        gateway_findings.extend(stats.get("gateway_prefix_warnings", []))
        gateway_notes.extend(stats.get("gateway_prefix_notes", []))
        if gateway_prefix:
            if gateway_prefix in gateway_prefix_map and gateway_prefix_map[gateway_prefix] != service_name:
                gateway_findings.append(
                    f"DUPLICATE gateway prefix {gateway_prefix!r}: {gateway_prefix_map[gateway_prefix]} and "
                    f"{service_name} — keeping the first; gateway calls to {service_name} will NOT resolve")
            else:
                gateway_prefix_map[gateway_prefix] = service_name
        if not shared:
            resolved = db.resolve_exact_import_edges("java")
            resolved += db.resolve_pending_edges(java_registry.as_dict(), project_type="java",
                                                  exclude_relations=("IMPORTS",))
            db.close()
            per_target_summaries.append((service_name, _db_path_for(config.output_dir, service_name),
                                          {**stats, **flow_stats, "resolved_edges": resolved}))
        else:
            per_target_summaries.append((service_name, None, {**stats, **flow_stats}))
        err_suffix = f", {stats['errors']} errors" if stats["errors"] else ""
        flow_suffix = f", {flow_stats['flow_edges']} flow edges" if flow_stats.get("flow_edges") else ""
        feign_suffix = f", {stats['feign_calls']} feign calls" if stats.get("feign_calls") else ""
        gw_suffix = (f", gateway_prefix={gateway_prefix!r} [{stats.get('gateway_prefix_source')}]" if gateway_prefix
                     else ", gateway_prefix=UNRESOLVED")
        _print(f"    -> {stats['files']} files, {stats['classes']} classes, "
               f"{stats['methods']} methods, {stats['endpoints']} endpoints{feign_suffix}"
               f"{flow_suffix}{err_suffix}{gw_suffix}")

    for note in gateway_notes:
        _print(f"[gateway] {note}")
    if gateway_findings:
        _print("\n[gateway] findings:")
        for note in gateway_findings:
            _print(f"  [WARN] {note}")
        if gateway_strict_mode():
            raise RuntimeError(f"KG_GATEWAY_STRICT: {len(gateway_findings)} gateway-prefix finding(s) — see above")

    if angular_subprojects:
        project_node_id = None
        if shared:
            project_node_id = angular_parser.parse_project(shared_db, config.angular_project, config.root_path)
        for raw_name in angular_subprojects:
            if raw_name == ROOT_APP_SENTINEL:
                subproject_name = f"{config.angular_project}-root-app"
                subproject_path = os.path.join(config.root_path, config.angular_project)
            else:
                # Incremental deletion + fingerprints are keyed by (file_path, service).
                # A split-out UI repo embeds its own copies of shared libraries
                # (clinical-casesheet, ...) at the same relative paths as the
                # monolith's, so an unqualified service name made each project's
                # run silently replace the previous one's nodes. Only the
                # monolith keeps bare names (existing tools/queries key on them);
                # every other root is qualified by its own directory name.
                project_dir = os.path.basename(config.angular_project.rstrip("/\\"))
                subproject_name = raw_name if project_dir == _PRIMARY_ANGULAR_PROJECT else f"{project_dir}/{raw_name}"
                subproject_path = os.path.join(config.root_path, config.angular_project, "projects", raw_name)
            if not os.path.isdir(subproject_path):
                _print(f"[skip] angular subproject not found on disk: {subproject_path}")
                continue
            db = shared_db if shared else GraphDB(_db_path_for(config.output_dir, f"{config.angular_project}_{subproject_name}"), incremental=config.incremental)
            if not shared:
                project_node_id = angular_parser.parse_project(db, config.angular_project, config.root_path)

            only_files = None
            if config.incremental and not db.is_fresh:
                src_dir = os.path.join(subproject_path, "src")
                walk_root = src_dir if os.path.isdir(src_dir) else subproject_path
                all_ts = list(walk_source_files(walk_root, _ANGULAR_EXTS, ignored_suffixes=_ANGULAR_IGNORED))
                changed, unchanged = incremental.classify_files(db, subproject_name, subproject_path, all_ts)
                removed = incremental.sweep_deleted(db, subproject_name, subproject_path, all_ts)
                only_files = set(changed)
                _print(f"[angular] indexing {subproject_name} (incremental: {len(changed)} changed, "
                       f"{len(unchanged)} unchanged, {removed} removed) ...")
            else:
                _print(f"[angular] indexing {subproject_name} (full) ...")

            stats = angular_parser.parse_subproject(
                db, ng_registry, config.angular_project, project_node_id,
                subproject_name, subproject_path, progress=_progress, only_files=only_files,
            )
            if not shared:
                resolved = db.resolve_pending_edges(ng_registry.as_dict(), project_type="angular")
                db.close()
                per_target_summaries.append((subproject_name,
                                              _db_path_for(config.output_dir, f"{config.angular_project}_{subproject_name}"),
                                              {**stats, "resolved_edges": resolved}))
            else:
                per_target_summaries.append((subproject_name, None, stats))
            err_suffix = f", {stats['errors']} errors" if stats["errors"] else ""
            api_const_suffix = f", {stats['API_CONSTANT']} API constants" if stats.get("API_CONSTANT") else ""
            _print(f"    -> {stats['files']} files, {stats['classes']} classes{api_const_suffix}{err_suffix}")

    if react_enabled:
        react_path = config.react_project_path
        if not os.path.isdir(react_path):
            _print(f"[skip] React project not found on disk: {react_path}")
        else:
            db = shared_db if shared else GraphDB(_db_path_for(config.output_dir, "react"), incremental=config.incremental)
            only_files = None
            if config.incremental and not db.is_fresh:
                all_react = list(walk_source_files(react_path, _REACT_EXTS, ignored_suffixes=_REACT_IGNORED))
                changed, unchanged = incremental.classify_files(db, "react", react_path, all_react)
                removed = incremental.sweep_deleted(db, "react", react_path, all_react)
                only_files = set(changed)
                _print(f"[react] indexing {react_path} (incremental: {len(changed)} changed, "
                       f"{len(unchanged)} unchanged, {removed} removed) ...")
            else:
                _print(f"[react] indexing {react_path} (full) ...")
            stats, project_name = react_parser.parse_project(db, react_registry, react_path,
                                                               progress=_progress, only_files=only_files)
            if not shared:
                resolved = db.resolve_pending_edges(react_registry.as_dict(), project_type="react")
                db.close()
                per_target_summaries.append((project_name, _db_path_for(config.output_dir, "react"),
                                              {**stats, "resolved_edges": resolved}))
            else:
                per_target_summaries.append((project_name, None, stats))
            err_suffix = f", {stats['errors']} errors" if stats["errors"] else ""
            _print(f"    -> {stats['files']} files, {stats['components']} components, "
                   f"{stats['hooks']} hooks{err_suffix}")

    if shared:
        resolved_java = shared_db.resolve_exact_import_edges("java")
        resolved_java += shared_db.resolve_pending_edges(java_registry.as_dict(), project_type="java",
                                                          exclude_relations=("IMPORTS",))
        resolved_ng = shared_db.resolve_pending_edges(ng_registry.as_dict(), project_type="angular")
        resolved_react = shared_db.resolve_pending_edges(react_registry.as_dict(), project_type="react")
        _print(f"\nResolved cross-reference edges: java={resolved_java} angular={resolved_ng} react={resolved_react}")

        _seed_gateway_prefix_map(shared_db, gateway_prefix_map)
        _print(f"Gateway prefixes discovered: {gateway_prefix_map}")
        resolved_calls = shared_db.resolve_service_call_edges(gateway_prefix_map)
        api_result = shared_db.resolve_api_constant_edges(gateway_prefix_map)
        _print(f"Resolved backend->backend calls (Feign/WebClient/RestTemplate): {resolved_calls}")
        _print(f"Resolved UI API constants -> backend endpoints: {api_result['resolved']} "
               f"({api_result['ambiguous']} ambiguous, {api_result['unresolved']} unresolved)")
    elif backend_services and (angular_subprojects or react_enabled):
        _print("\n[note] SINGLE_DATABASE=false — UI API constant -> backend endpoint and "
               "backend->backend (Feign/WebClient/RestTemplate) resolution are skipped: "
               "they require a shared graph to see nodes across service/subproject "
               "boundaries. Set SINGLE_DATABASE=true to enable them.")

    return per_target_summaries


def _print_summary(label, db_path, summaries):
    _print("")
    _print("-" * 64)
    _print(f"Summary - {label}")
    _print("-" * 64)
    totals = {}
    for name, _path, stats in summaries:
        for key, value in stats.items():
            if isinstance(value, (int, float)):
                totals[key] = totals.get(key, 0) + value
    for key in sorted(totals):
        _print(f"  {key:14s}: {totals[key]}")
    _print(f"\n  Targets indexed : {len(summaries)}")
    _print(f"  Database file   : {db_path}")
