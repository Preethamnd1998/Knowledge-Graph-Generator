import os
import time

from .config import Config
from .db import GraphDB
from .parsers.common import NameRegistry
from .parsers import angular_parser, java_parser, react_parser, spring_flow_parser
from .parsers.angular_parser import ROOT_APP_SENTINEL


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
        db = GraphDB(db_path)
        _print(f"Writing combined graph to: {db_path}\n")
        summaries = _index_all(config, db, backend_services, angular_subprojects, react_enabled, shared=True)
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


def _index_all(config, shared_db, backend_services, angular_subprojects, react_enabled, shared):
    java_registry = NameRegistry()
    ng_registry = NameRegistry()
    react_registry = NameRegistry()
    per_target_summaries = []

    for service_name in backend_services:
        service_path = os.path.join(config.root_path, service_name)
        if not os.path.isdir(service_path):
            _print(f"[skip] backend service not found on disk: {service_path}")
            continue
        _print(f"[java] indexing {service_name} ...")
        db = shared_db if shared else GraphDB(_db_path_for(config.output_dir, service_name))
        stats = java_parser.parse_service(db, java_registry, service_name, service_path, progress=_progress)
        flow_stats = spring_flow_parser.parse_service(db, java_registry, service_name, service_path)
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
        _print(f"    -> {stats['files']} files, {stats['classes']} classes, "
               f"{stats['methods']} methods, {stats['endpoints']} endpoints{flow_suffix}{err_suffix}")

    if angular_subprojects:
        project_node_id = None
        if shared:
            project_node_id = angular_parser.parse_project(shared_db, config.angular_project, config.root_path)
        for raw_name in angular_subprojects:
            if raw_name == ROOT_APP_SENTINEL:
                subproject_name = f"{config.angular_project}-root-app"
                subproject_path = os.path.join(config.root_path, config.angular_project)
            else:
                subproject_name = raw_name
                subproject_path = os.path.join(config.root_path, config.angular_project, "projects", raw_name)
            if not os.path.isdir(subproject_path):
                _print(f"[skip] angular subproject not found on disk: {subproject_path}")
                continue
            _print(f"[angular] indexing {subproject_name} ...")
            db = shared_db if shared else GraphDB(_db_path_for(config.output_dir, f"{config.angular_project}_{subproject_name}"))
            if not shared:
                project_node_id = angular_parser.parse_project(db, config.angular_project, config.root_path)
            stats = angular_parser.parse_subproject(
                db, ng_registry, config.angular_project, project_node_id,
                subproject_name, subproject_path, progress=_progress,
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
            _print(f"    -> {stats['files']} files, {stats['classes']} classes{err_suffix}")

    if react_enabled:
        react_path = config.react_project_path
        if not os.path.isdir(react_path):
            _print(f"[skip] React project not found on disk: {react_path}")
        else:
            _print(f"[react] indexing {react_path} ...")
            db = shared_db if shared else GraphDB(_db_path_for(config.output_dir, "react"))
            stats, project_name = react_parser.parse_project(db, react_registry, react_path, progress=_progress)
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
