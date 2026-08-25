"""Spring Batch / Kafka / async-execution flow parser.

An ADDITIVE second pass over the same Java source java_parser.py already
walked for a service. Must run after java_parser.parse_service() for that
service (it resolves against CLASS/METHOD nodes java_parser already
created via db.resolve() on the identical qualified_name formula) and
before the service's resolve_pending_edges() call (it registers new bean
names into the same NameRegistry so cross-file references — mainly
JobLauncher.run() targets — get resolved by that existing mechanism
instead of a bespoke one).

New node types (see README/AGENTS.md for the base schema this extends):
    BATCH_JOB, BATCH_STEP, BATCH_TASKLET, ASYNC_OPERATION,
    KAFKA_TOPIC, DB_TABLE

SPRING_CONFIG / SPRING_BEAN / KAFKA_PRODUCER / KAFKA_CONSUMER are
intentionally NOT separate node types. A @Configuration class and a
@Bean method already exist as CLASS/METHOD nodes with that annotation
in their metadata — a parallel node would duplicate data with no new
query value. Likewise a Kafka producer/consumer is just the real
class/method that calls publishToKafka(...) or carries @KafkaListener;
the edge (PUBLISHES/CONSUMES) captures the role, not a synthetic node.

New edge relations: DEFINES_BEAN, HAS_STEP, USES_TASKLET, RUNS,
ASYNC_CALL, PUBLISHES, CONSUMES, WRITES, READS.

Confidence model (kept in metadata["confidence"] on every edge this
module creates, on top of — not instead of — the base graph's existing
implicit convention that target_id being set means resolved):
    verified  - read directly off an explicit annotation (@Bean,
                @Async, @KafkaListener) with no pattern-matching guess.
    inferred  - recognized from a framework/builder convention
                (JobBuilder.start(x), producer-call topic argument,
                SQL keyword table extraction, @Qualifier bean-name
                resolution) — a reasonable, but not language-guaranteed,
                interpretation.
    unknown   - referenced in source but not resolvable to a real node
                (target_id stays NULL, target_name preserved). Never
                silently upgraded to a guess.
"""
import os
import re

from .common import find_matching, line_of, read_text, split_top_level, strip_type, walk_source_files
from .java_parser import (
    CLASS_DECL_RE, METHOD_RE, PACKAGE_RE, PARAM_ANNOTATION_PREFIX_RE,
    _find_block_end, _parse_annotations, _split_types,
)

BEAN_RETURN_TYPES = {"Job": "BATCH_JOB", "Step": "BATCH_STEP", "Tasklet": "BATCH_TASKLET"}

QUALIFIER_FIELD_RE = re.compile(
    r'@Qualifier\(\s*"([^"]+)"\s*\)\s*(?:\r?\n\s*)?'
    r'(?:@\w[\w.]*(?:\((?:[^()]|\([^()]*\))*\))?\s*(?:\r?\n\s*)?)*'
    r'(?:private\s+|protected\s+|public\s+|final\s+)*[\w.<>,\[\] ]+?\s+(\w+)\s*[;=]'
)

JOB_STEP_REF_RE = re.compile(r'\.\s*(?:start|next)\s*\(\s*(\w+)\s*\(')
STEP_TASKLET_REF_RE = re.compile(r'\.\s*tasklet\s*\(\s*(\w+)\s*\(')
JOB_LAUNCHER_RUN_RE = re.compile(r'\bjobLauncher\w*\s*\.\s*run\s*\(\s*(?:this\.)?(\w+)')
ASYNC_CALL_RE = re.compile(r'\bCompletableFuture\s*\.\s*(runAsync|supplyAsync)\s*\(')
PUBLISH_CALL_RE = re.compile(r'\b(\w+)\s*\.\s*publishToKafka\s*\(')
SEND_LITERAL_RE = re.compile(r'\b(\w+)\s*\.\s*(send|sendDefault)\s*\(\s*"([^"]+)"')

SQL_TABLE_PATTERNS = (
    # (?i:...) scopes case-insensitivity to just the SQL keywords — the
    # captured table name itself stays [a-z] case-SENSITIVE on purpose,
    # so a JPQL/HQL entity reference ("FROM TempPriceBook", PascalCase)
    # can never be mistaken for a real lowercase/snake_case SQL table.
    ("WRITES", re.compile(r'\b(?i:INSERT\s+INTO)\s+([a-z][a-z0-9_]*)\b'), "insert"),
    ("WRITES", re.compile(r'\b(?i:UPDATE)\s+([a-z][a-z0-9_]*)\s+(?i:SET)\b'), "update"),
    ("WRITES", re.compile(r'\b(?i:DELETE\s+FROM)\s+([a-z][a-z0-9_]*)\b'), "delete"),
    ("READS", re.compile(r'\b(?i:SELECT)\b(?:(?!;).)*?\b(?i:FROM)\s+([a-z][a-z0-9_]*)\b'), "select"),
)

_TOPIC_TOKEN_RE = re.compile(r'"([^"]+)"|([A-Za-z_][\w.]*)')


def _meta(confidence, **extra):
    return {"confidence": confidence, **extra}


def _compute_param_types(params_raw):
    """Identical to java_parser._parse_method's own logic — must stay
    byte-for-byte the same so the qualified_name computed here matches
    the METHOD node java_parser already inserted, for db.resolve()."""
    types = []
    for param in _split_types(params_raw):
        param = param.strip()
        if not param:
            continue
        param = param.replace("final ", "")
        param = PARAM_ANNOTATION_PREFIX_RE.sub("", param)
        pieces = param.rsplit(" ", 1)
        types.append(strip_type(pieces[0]) if len(pieces) > 1 else strip_type(param))
    return types


def _normalize_topic_token(raw):
    return raw.rsplit(".", 1)[-1]


def _extract_topics_from_kafka_listener_args(args_text):
    m = re.search(r'topics\s*=\s*(\{[^}]*\}|"[^"]*"|[\w.]+)', args_text or "")
    segment = m.group(1) if m else (args_text or "")
    topics, seen = [], set()
    for tm in _TOPIC_TOKEN_RE.finditer(segment):
        raw = tm.group(1) or tm.group(2)
        if not raw:
            continue
        token = _normalize_topic_token(raw)
        if token not in seen:
            seen.add(token)
            topics.append(token)
    return topics


def _qualifier_field_map(class_body_text):
    """{field_name: qualifier_bean_name} for fields annotated
    @Qualifier("beanName") in this class — the only reliable way to know
    which bean a jobLauncher.run(this.someField, ...) call actually
    targets, since the field's declared type (Job) isn't itself a bean
    identifier."""
    return {fm.group(2): fm.group(1) for fm in QUALIFIER_FIELD_RE.finditer(class_body_text)}


def _get_or_create_topic(db, topic_key, service_name):
    """Kafka topic identity is global (not scoped to one service) so a
    producer in service A and a consumer in service B resolve to the
    SAME node — add_node's qualified_name dedup handles this for free
    regardless of which service's parse call creates it first."""
    return db.add_node(
        "KAFKA_TOPIC", topic_key, qualified_name=f"kafka-topic:{topic_key}",
        metadata={"confidence": "inferred"},
    )


def _get_or_create_table(db, table_name, service_name):
    return db.add_node(
        "DB_TABLE", table_name, qualified_name=f"db-table:{service_name}:{table_name}",
        service=service_name, project_type="java",
        metadata={"confidence": "inferred"},
    )


class _MethodInfo:
    __slots__ = ("name", "qname", "return_type", "annotations", "annotation_args",
                 "body_text", "start_line", "end_line")

    def __init__(self, name, qname, return_type, annotations, annotation_args,
                 body_text, start_line, end_line):
        self.name = name
        self.qname = qname
        self.return_type = return_type
        self.annotations = annotations
        self.annotation_args = annotation_args
        self.body_text = body_text
        self.start_line = start_line
        self.end_line = end_line


def _collect_methods(full_text, class_body_start, class_body_text, class_qname):
    methods = []
    for mm in METHOD_RE.finditer(class_body_text):
        name = mm.group("name")
        return_type = strip_type(mm.group("return_type"))
        param_types = _compute_param_types(mm.group("params"))
        qname = f"{class_qname}#{name}({','.join(t or '' for t in param_types)})"
        annotations = _parse_annotations(mm.group("annotations"))

        method_body_open = class_body_start + mm.end() - 1
        method_body_end = _find_block_end(full_text, method_body_open)
        body_text = full_text[method_body_open:method_body_end]

        methods.append(_MethodInfo(
            name=name, qname=qname, return_type=return_type,
            annotations=[a["name"] for a in annotations],
            annotation_args={a["name"]: a["args"] for a in annotations},
            body_text=body_text,
            start_line=line_of(full_text, class_body_start + mm.start()),
            end_line=line_of(full_text, method_body_end),
        ))
    return methods


def _parse_class_flows(db, registry, service_name, rel_file, full_text, cm, stats):
    name = cm.group("name")
    pkg_match = PACKAGE_RE.search(full_text)
    package = pkg_match.group(1) if pkg_match else ""
    class_qname = f"{package}.{name}" if package else name

    class_node_id = db.resolve(class_qname)
    if class_node_id is None:
        return  # java_parser didn't create this class node (parse error) — nothing to attach to

    class_annotations = [a["name"] for a in _parse_annotations(cm.group("annotations"))]
    is_config = "Configuration" in class_annotations

    class_body_start = cm.end() - 1
    class_body_end = _find_block_end(full_text, class_body_start)
    class_body_text = full_text[class_body_start:class_body_end]

    field_qualifiers = _qualifier_field_map(class_body_text)
    methods = _collect_methods(full_text, class_body_start, class_body_text, class_qname)

    # Pass A: create every bean-shaped node first, so Pass B can resolve
    # forward references within the same class (a Job bean commonly
    # references a Step bean defined *later* in the same config class).
    bean_nodes = {}  # method name -> node_id
    for m in methods:
        if "Bean" not in m.annotations:
            continue
        node_type = BEAN_RETURN_TYPES.get(m.return_type)
        if not node_type:
            continue
        node_id = db.add_node(
            node_type, m.name, qualified_name=f"{node_type.lower().replace('_', '-')}:{service_name}:{m.qname}",
            service=service_name, project_type="java", file_path=rel_file,
            start_line=m.start_line, end_line=m.end_line,
            metadata=_meta("verified", bean_method=m.qname),
        )
        bean_nodes[m.name] = node_id
        registry.register(m.name, f"{node_type.lower().replace('_', '-')}:{service_name}:{m.qname}")
        db.add_edge(class_node_id, "DEFINES_BEAN", target_id=node_id, service=service_name,
                    metadata=_meta("verified"))
        stats[node_type] = stats.get(node_type, 0) + 1

    # Pass B: builder-pattern links (Job->Step, Step->Tasklet), JobLauncher
    # calls, async execution, Kafka producer/consumer, DB table refs.
    for m in methods:
        if m.name in bean_nodes:
            node_type = BEAN_RETURN_TYPES.get(m.return_type)
            source_id = bean_nodes[m.name]
            if node_type == "BATCH_JOB":
                for sm in JOB_STEP_REF_RE.finditer(m.body_text):
                    _link_bean_ref(db, source_id, "HAS_STEP", sm.group(1), bean_nodes,
                                    service_name, stats)
            elif node_type == "BATCH_STEP":
                for tm in STEP_TASKLET_REF_RE.finditer(m.body_text):
                    _link_bean_ref(db, source_id, "USES_TASKLET", tm.group(1), bean_nodes,
                                    service_name, stats)

        if "Async" in m.annotations:
            async_id = db.add_node(
                "ASYNC_OPERATION", m.name, qualified_name=f"async:{service_name}:{m.qname}",
                service=service_name, project_type="java", file_path=rel_file,
                start_line=m.start_line, metadata=_meta("verified", via="@Async"),
            )
            db.add_edge(class_node_id, "DECLARES", target_id=async_id,
                        service=service_name, metadata=_meta("verified"))
            stats["ASYNC_OPERATION"] = stats.get("ASYNC_OPERATION", 0) + 1

        method_node_id = db.resolve(m.qname)
        _scan_method_body(db, registry, service_name, rel_file, m, method_node_id,
                           class_node_id, field_qualifiers, stats)


def _link_bean_ref(db, source_id, relation, ref_name, bean_nodes, service_name, stats):
    target_id = bean_nodes.get(ref_name)
    if target_id is not None:
        db.add_edge(source_id, relation, target_id=target_id, service=service_name,
                    metadata=_meta("inferred"))
    else:
        db.add_edge(source_id, relation, target_name=ref_name, service=service_name,
                    metadata=_meta("unknown"))
    stats["flow_edges"] = stats.get("flow_edges", 0) + 1


def _scan_method_body(db, registry, service_name, rel_file, m, method_node_id,
                       class_node_id, field_qualifiers, stats):
    source_id = method_node_id if method_node_id is not None else class_node_id
    if source_id is None:
        return

    async_hits = list(ASYNC_CALL_RE.finditer(m.body_text))
    for idx, am in enumerate(async_hits):
        async_id = db.add_node(
            "ASYNC_OPERATION", f"{m.name}#async{idx}",
            qualified_name=f"async:{service_name}:{m.qname}#{idx}",
            service=service_name, project_type="java", file_path=rel_file,
            start_line=m.start_line, metadata=_meta("inferred", via=f"CompletableFuture.{am.group(1)}"),
        )
        db.add_edge(source_id, "ASYNC_CALL", target_id=async_id, service=service_name,
                    metadata=_meta("inferred"))
        stats["ASYNC_OPERATION"] = stats.get("ASYNC_OPERATION", 0) + 1

    for jm in JOB_LAUNCHER_RUN_RE.finditer(m.body_text):
        raw_ref = jm.group(1)
        job_name = field_qualifiers.get(raw_ref, raw_ref)
        db.add_edge(source_id, "RUNS", target_name=job_name, service=service_name,
                    metadata=_meta("inferred", raw_reference=raw_ref))
        stats["flow_edges"] = stats.get("flow_edges", 0) + 1

    for pm in PUBLISH_CALL_RE.finditer(m.body_text):
        open_idx = pm.end() - 1
        close_idx = find_matching(m.body_text, open_idx, "(", ")")
        args = split_top_level(m.body_text[open_idx + 1:close_idx - 1], ",")
        if len(args) >= 2:
            topic_key = _normalize_topic_token(args[1].strip())
            topic_id = _get_or_create_topic(db, topic_key, service_name)
            db.add_edge(source_id, "PUBLISHES", target_id=topic_id, service=service_name,
                        metadata=_meta("inferred", via="publishToKafka"))
            stats["KAFKA_TOPIC"] = stats.get("KAFKA_TOPIC", 0) + 1
            stats["flow_edges"] = stats.get("flow_edges", 0) + 1

    for sm in SEND_LITERAL_RE.finditer(m.body_text):
        topic_key = sm.group(3)
        topic_id = _get_or_create_topic(db, topic_key, service_name)
        db.add_edge(source_id, "PUBLISHES", target_id=topic_id, service=service_name,
                    metadata=_meta("inferred", via=sm.group(2)))
        stats["KAFKA_TOPIC"] = stats.get("KAFKA_TOPIC", 0) + 1
        stats["flow_edges"] = stats.get("flow_edges", 0) + 1

    if "KafkaListener" in m.annotations:
        topics = _extract_topics_from_kafka_listener_args(m.annotation_args.get("KafkaListener"))
        for topic_key in topics:
            topic_id = _get_or_create_topic(db, topic_key, service_name)
            db.add_edge(source_id, "CONSUMES", target_id=topic_id, service=service_name,
                        metadata=_meta("verified", via="@KafkaListener"))
            stats["KAFKA_TOPIC"] = stats.get("KAFKA_TOPIC", 0) + 1
            stats["flow_edges"] = stats.get("flow_edges", 0) + 1

    for relation, pattern, op in SQL_TABLE_PATTERNS:
        for tm in pattern.finditer(m.body_text):
            table_id = _get_or_create_table(db, tm.group(1), service_name)
            db.add_edge(source_id, relation, target_id=table_id, service=service_name,
                        metadata=_meta("inferred", operation=op))
            stats["DB_TABLE"] = stats.get("DB_TABLE", 0) + 1
            stats["flow_edges"] = stats.get("flow_edges", 0) + 1


def parse_service(db, registry, service_name, service_path, progress=None):
    """Run after java_parser.parse_service() for the same service, before
    that service's resolve_pending_edges() call — see module docstring."""
    stats = {"flow_edges": 0}

    for file_path in walk_source_files(service_path, (".java",), ignored_suffixes=("Test.java", "Tests.java")):
        text = read_text(file_path)
        if text is None:
            continue
        rel_file = os.path.relpath(file_path, service_path)
        for cm in CLASS_DECL_RE.finditer(text):
            try:
                _parse_class_flows(db, registry, service_name, rel_file, text, cm, stats)
            except Exception:
                stats["flow_errors"] = stats.get("flow_errors", 0) + 1
        if progress:
            progress(service_name, file_path, stats)

    return stats
