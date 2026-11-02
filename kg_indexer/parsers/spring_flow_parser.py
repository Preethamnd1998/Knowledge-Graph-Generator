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
ASYNC_CALL, PUBLISHES, CONSUMES, WRITES, READS, WEBCLIENT_CALL,
RESTTEMPLATE_CALL.

WEBCLIENT_CALL / RESTTEMPLATE_CALL (backend -> backend, imperative call
sites — the counterpart of java_parser.py's declarative FEIGN_CALL):
recognized only where the target is statically known from source —
WebClient via a `.baseUrl("literal")` builder call found anywhere in the
same class (best-effort: a class with more than one WebClient field with
different base URLs isn't disambiguated, hence "inferred" not "verified"),
RestTemplate only where the call's first argument is itself a literal
`"http://host/path"` string (a `@Value`-injected base URL, common in this
codebase for genuinely external hosts like a license server, is
deliberately left unresolved rather than guessed at). Both resolve against
the same gateway-prefix map as FEIGN_CALL, via
GraphDB.resolve_service_call_edges().

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
import json
import re

from .common import find_matching, line_of, mask_java_comments, read_text, split_top_level, strip_type, walk_source_files
from .java_parser import (
    CLASS_DECL_RE, FIELD_DEP_RE, FINAL_FIELD_DEP_RE, METHOD_RE, PACKAGE_RE, PARAM_ANNOTATION_PREFIX_RE,
    _NOT_A_DEPENDENCY_TYPES, _find_block_end, _parse_annotations, _split_types,
)

# `dep.method(` / `this.dep.method(` where `dep` is an injected field. The field's
# DECLARED type gives the callee class exactly — the same resolution Spring does —
# so this is a typed call edge (CALLS_DEP), not a name guess. Code Cortex often
# misses these or binds them by name (confidence 0.01-0.55).
_DEP_CALL_RE = re.compile(r'(?<![\w.])(?:this\s*\.\s*)?(\w+)\s*\.\s*(\w+)\s*\(')
_DEP_CALL_SKIP = {"equals", "hashCode", "toString", "getClass", "notify", "notifyAll", "wait", "clone"}


def _dep_fields(class_body_text):
    """{injected field name: declared type} for @Autowired/@Inject/@Resource and constructor-injected fields."""
    fields = {}
    for rx in (FIELD_DEP_RE, FINAL_FIELD_DEP_RE):
        for fm in rx.finditer(class_body_text):
            t = strip_type(fm.group("type"))
            if t and t not in _NOT_A_DEPENDENCY_TYPES:
                fields[fm.group("name")] = t
    return fields

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
    # `(?:[a-z_][a-z0-9_]*\.)?` skips an optional schema qualifier so
    # `INSERT INTO public.serial_number` yields `serial_number`, not `public`.
    ("WRITES", re.compile(r'\b(?i:INSERT\s+INTO)\s+(?:[a-z_][a-z0-9_]*\.)?([a-z][a-z0-9_]*)\b'), "insert"),
    ("WRITES", re.compile(r'\b(?i:UPDATE)\s+(?:[a-z_][a-z0-9_]*\.)?([a-z][a-z0-9_]*)\s+(?i:SET)\b'), "update"),
    ("WRITES", re.compile(r'\b(?i:DELETE\s+FROM)\s+(?:[a-z_][a-z0-9_]*\.)?([a-z][a-z0-9_]*)\b'), "delete"),
    # READS is handled by _select_tables (below), not a single regex: the first
    # FROM after a SELECT is often a sub-select or a function
    # (`(SELECT json_agg(..) FROM jsonb_array_elements(..)) ... FROM real_table`).
)

_SELECT_SPAN_RE = re.compile(r'\b(?i:SELECT)\b([^;]*)')          # [^;] (unlike '.') crosses newlines / text blocks
_FROM_JOIN_RE = re.compile(r'\b(?i:FROM|JOIN)\s+(?:[a-z_][a-z0-9_]*\.)?([a-z][a-z0-9_]*)\b(?!\s*\()')  # not `func(`
_DELETE_FROM_RE = re.compile(r'\b(?i:DELETE)\s+(?i:FROM)\s+(?:[a-z_][a-z0-9_]*\.)?[a-z][a-z0-9_]*')
_PROSE_WORDS = {"the", "a", "an", "this", "that", "these", "those", "which", "it", "them", "there", "each",
                "all", "any", "one", "other", "list", "set", "map", "here", "now", "user", "table"}


def _select_tables(text):
    """Every table in FROM/JOIN clauses within one SELECT statement (up to `;`),
    excluding function calls and English prose from comments/log messages."""
    tables = []
    for sm in _SELECT_SPAN_RE.finditer(text):
        span = _DELETE_FROM_RE.sub("", sm.group(1))
        tables.extend(fm.group(1) for fm in _FROM_JOIN_RE.finditer(span) if fm.group(1) not in _PROSE_WORDS)
    return tables

_TOPIC_TOKEN_RE = re.compile(r'"([^"]+)"|([A-Za-z_][\w.]*)')

WEBCLIENT_BASEURL_RE = re.compile(r'\.baseUrl\(\s*"([^"]+)"\s*\)')
WEBCLIENT_CALL_RE = re.compile(
    r'\bwebClient\w*\s*\.\s*(get|post|put|patch|delete)\s*\(\s*\)'
    r'(?P<between>.{0,400}?)'
    r'\.path\(\s*"([^"]+)"',
    re.DOTALL,
)
_HOST_STRIP_RE = re.compile(r'^https?://')

RESTTEMPLATE_METHOD_TO_HTTP = {
    "getForEntity": "GET", "getForObject": "GET",
    "postForEntity": "POST", "postForObject": "POST",
    "put": "PUT", "delete": "DELETE", "patchForObject": "PATCH",
}
RESTTEMPLATE_CALL_RE = re.compile(
    r'\brestTemplate\w*\s*\.\s*(getForEntity|postForEntity|getForObject|postForObject|'
    r'exchange|put|delete|patchForObject)\s*\(\s*"(https?://[^"/]+)(/[^"]*)?"'
)
RESTTEMPLATE_EXCHANGE_METHOD_RE = re.compile(r'HttpMethod\.(\w+)')


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


_TOPIC_ACCESSOR_RE = re.compile(r'\.\s*(?:toString|name)\s*\(\s*\)\s*$')
_CONSTANT_RE = re.compile(r'^[A-Z][A-Z0-9_]+$')


def _normalize_topic_token(raw):
    """`KafkaTopics.LT_X` / `KafkaTopics.LT_X.toString()` / `.name()` -> `LT_X`
    (the enum-constant name, which is how consumers' @KafkaListener topics are
    normalized too, so both sides link). `someVar.toString()` is a runtime
    expression, not a topic: it is left as-is (unresolved), never guessed."""
    raw = raw.strip()
    unwrapped = _TOPIC_ACCESSOR_RE.sub("", raw)
    if unwrapped != raw and not _CONSTANT_RE.match(unwrapped.rsplit(".", 1)[-1]):
        return raw.rsplit(".", 1)[-1]
    return unwrapped.rsplit(".", 1)[-1]


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


_APPEND_LITERAL_RE = re.compile(r'\b(\w+)\s*\.\s*append\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
_LITERAL_CONCAT_RE = re.compile(r'"\s*\+\s*"')


def _sql_candidate_texts(body_text):
    """The method body itself, plus SQL re-assembled from fragments: the
    `StringBuilder sql; sql.append("SELECT ..."); sql.append("FROM tbl ...")`
    style and `"SELECT ..." + \\n "FROM tbl"` concatenations put SELECT and FROM
    in different string literals (often different lines), which the per-line
    SQL patterns can't see. Fragments are only joined within ONE builder
    variable / one `+` chain — never across unrelated literals (log messages)."""
    texts = [body_text]
    by_var = {}
    for am in _APPEND_LITERAL_RE.finditer(body_text):
        by_var.setdefault(am.group(1), []).append(am.group(2))
    texts.extend(" ".join(parts) for parts in by_var.values() if len(parts) > 1)
    collapsed = _LITERAL_CONCAT_RE.sub("", body_text)
    if collapsed != body_text:
        texts.append(collapsed)
    return texts


# Table access mechanisms, strongest first. A table reached through several
# mechanisms keeps ALL of them in metadata["engines"]; metadata["engine"] is the
# strongest NON-sql one (absent for sql-only, which readers treat as "sql").
_ENGINE_PRECEDENCE = ("mongo", "jpa")


def _merge_engine_metadata(meta, engine):
    engines = set(meta.get("engines") or ([meta["engine"]] if meta.get("engine") else []))
    engines.add(engine or "sql")
    meta["engines"] = sorted(engines)
    strongest = next((e for e in _ENGINE_PRECEDENCE if e in engines), None)
    if strongest:
        meta["engine"] = strongest
    else:
        meta.pop("engine", None)
    return meta


def _get_or_create_table(db, table_name, service_name, engine=None):
    """One DB_TABLE node per (service, table). add_node returns the EXISTING node
    untouched, so whichever access path (JPA repository, raw SQL, Mongo) happened
    to reach the table first used to decide its metadata for good — a SQL hit
    parsed before the JPA repository then hid engine=jpa. Merge instead."""
    qname = f"db-table:{service_name}:{table_name}"
    existing = db.resolve(qname)
    if existing is None:
        meta = _merge_engine_metadata({"confidence": "inferred"}, engine)
        return db.add_node("DB_TABLE", table_name, qualified_name=qname, service=service_name,
                           project_type="java", metadata=meta)
    row = db.conn.execute("SELECT metadata FROM nodes WHERE id = ?", (existing,)).fetchone()
    try:
        meta = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        meta = {}
    merged = _merge_engine_metadata(dict(meta), engine)
    if merged != meta:
        db.conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(merged), existing))
    return existing


# --- Spring Data MongoDB persistence ----------------------------------------
# SQL_TABLE_PATTERNS above only sees SQL keywords in method bodies, so a
# Mongo-backed service (ipd-clinical, clinical-common, ...) previously got no
# persistence edges at all — a saga trace dead-ended before the save. Model:
#   @Document(collection="x") entity  ->  interface R extends MongoRepository<Entity,ID>
#   ->  `repoField.save/find/...(...)` inside a method body, plus
#   `mongoTemplate.<op>(..., Entity.class | "collection")`.
# Collections reuse the DB_TABLE node type (metadata engine="mongo") so every
# existing WRITES/READS/DB_TABLE consumer works unchanged. Confidence is
# "inferred" (name-convention + type resolution, not a compiler); a template
# call whose collection isn't statically visible stays "unknown", unresolved.
_MONGO_REPO_RE = re.compile(
    r'\b(MongoRepository|ReactiveMongoRepository|JpaRepository|CrudRepository|PagingAndSortingRepository|'
    r'ListCrudRepository|ListPagingAndSortingRepository|ReactiveCrudRepository)\s*<\s*(\w+)')
_TABLE_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')


def _snake_case(class_name):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()  # Spring's default naming strategy
_DOCUMENT_LITERAL_RE = re.compile(r'"([^"]+)"')
_MONGO_TEMPLATE_FIELD_RE = re.compile(r'\b(?:Reactive)?MongoTemplate\s+(\w+)\s*[;=,)]')
_ENTITY_CLASS_ARG_RE = re.compile(r'\b(\w+)\.class\b')
_HELPER_READ_RE = re.compile(r'find|count|exist|aggregate|fetch|query|search', re.I)
_STRING_ARG_RE = re.compile(r'"([A-Za-z0-9_.\-]+)"')

_MONGO_WRITE_EXACT = {
    "save": "save", "saveAll": "save", "saveAndFlush": "save", "saveAllAndFlush": "save",
    "insert": "insert", "insertAll": "insert",
    "upsert": "update", "findAndModify": "update", "findAndReplace": "update",
    "findAndRemove": "delete", "bulkOps": "update",
}
_MONGO_WRITE_PREFIXES = (("delete", "delete"), ("remove", "delete"), ("update", "update"),
                         ("save", "save"), ("insert", "insert"))
_MONGO_READ_PREFIXES = ("find", "get", "read", "query", "count", "exists", "stream", "aggregate", "select")


def _mongo_op(method_name):
    """(relation, operation) for a repository/template method name, or None
    when the name doesn't clearly read or write (never guess)."""
    if method_name in _MONGO_WRITE_EXACT:
        return "WRITES", _MONGO_WRITE_EXACT[method_name]
    for prefix, op in _MONGO_WRITE_PREFIXES:
        if method_name.startswith(prefix):
            return "WRITES", op
    for prefix in _MONGO_READ_PREFIXES:
        if method_name.startswith(prefix):
            return "READS", "select"
    return None


def _build_mongo_model(service_path):
    """Service-wide pre-pass: {entity: collection} and {repo: entity}."""
    entity_collection = {}
    entity_table = {}
    repo_entity = {}
    repo_parents = {}
    for file_path in walk_source_files(service_path, (".java",), ignored_suffixes=("Test.java", "Tests.java")):
        text = read_text(file_path)
        if text is None or ("Document" not in text and "Repository" not in text and "@Entity" not in text and "CompoundIndex" not in text):
            continue
        for cm in CLASS_DECL_RE.finditer(mask_java_comments(text)):
            name = cm.group("name")
            anns = _parse_annotations(cm.group("annotations"))
            names = {a["name"] for a in anns}
            for a in anns:
                if a["name"] != "Document":
                    continue
                lit = _DOCUMENT_LITERAL_RE.search(a["args"])
                if lit:
                    entity_collection[name] = lit.group(1)
                elif not a["args"].strip():
                    entity_collection[name] = name[0].lower() + name[1:]  # Spring Data default
                else:
                    entity_collection[name] = a["args"].strip()  # symbolic constant — kept verbatim, not guessed
            if names & {"CompoundIndexes", "CompoundIndex"} and name not in entity_collection and cm.group("kind") == "class":
                entity_collection[name] = name[0].lower() + name[1:]  # Mongo-only annotation, no @Document: Spring Data default
            if "Entity" in names and cm.group("kind") == "class":
                table_ann = next((a for a in anns if a["name"] == "Table"), None)
                if table_ann is None:
                    entity_table[name] = _snake_case(name)          # default naming strategy (inferred)
                else:
                    tm = _TABLE_NAME_RE.search(table_ann["args"])
                    if tm:
                        entity_table[name] = tm.group(1)            # a symbolic @Table(name=CONST) is left out, not guessed
            extends = cm.group("extends")
            if cm.group("kind") == "interface" and extends:
                rm = _MONGO_REPO_RE.search(extends)
                if rm:
                    repo_entity[name] = (rm.group(1), rm.group(2))
                else:
                    repo_parents[name] = re.findall(r"\w+", extends)
    # Transitive repos (interface Foo extends BaseMongoRepo)
    for _ in range(3):
        for repo, parents in repo_parents.items():
            if repo in repo_entity:
                continue
            for p in parents:
                if p in repo_entity:
                    repo_entity[repo] = repo_entity[p]
                    break
    # CrudRepository/PagingAndSortingRepository are shared by Mongo and JPA —
    # the entity decides: @Document -> Mongo collection, @Entity -> SQL table.
    repos = {}
    for repo, (base, entity) in repo_entity.items():
        if base.endswith("MongoRepository") or entity in entity_collection:
            repos[repo] = (entity, "mongo")
        elif entity in entity_table:
            repos[repo] = (entity, "jpa")
    for entity, engine in repos.values():
        if engine == "mongo":
            entity_collection.setdefault(entity, entity[0].lower() + entity[1:])
    return {"entity_collection": entity_collection, "entity_table": entity_table, "repo_entity": repos}


def _mongo_fields(class_body_text, model):
    """{field_name: RepoType} for injected repositories, plus MongoTemplate field names."""
    repo_fields = {}
    for repo in model["repo_entity"]:
        if repo in class_body_text:
            for fm in re.finditer(r'\b' + re.escape(repo) + r'\s+(\w+)\s*[;=,)]', class_body_text):
                repo_fields[fm.group(1)] = repo
    templates = {m.group(1) for m in _MONGO_TEMPLATE_FIELD_RE.finditer(class_body_text)}
    return repo_fields, templates


def _scan_mongo(db, service_name, source_id, body_text, repo_fields, template_fields, model, stats):
    seen = set()

    def _emit(relation, op, collection, via, entity=None, engine="mongo"):
        key = (relation, collection, op, via)
        if key in seen:
            return
        seen.add(key)
        if collection is None:
            db.add_edge(source_id, relation, target_name=f"{engine}:unresolved:{via}", service=service_name,
                        metadata=_meta("unknown", operation=op, engine=engine, via=via))
        else:
            table_id = _get_or_create_table(db, collection, service_name, engine=engine)
            extra = {"entity": entity} if entity else {}
            db.add_edge(source_id, relation, target_id=table_id, service=service_name,
                        metadata=_meta("inferred", operation=op, engine=engine, via=via, **extra))
            stats["DB_TABLE"] = stats.get("DB_TABLE", 0) + 1
        stats["flow_edges"] = stats.get("flow_edges", 0) + 1
        stats["MONGO_EDGES" if engine == "mongo" else "JPA_EDGES"] = stats.get(
            "MONGO_EDGES" if engine == "mongo" else "JPA_EDGES", 0) + 1

    for var, repo in repo_fields.items():
        entity, engine = model["repo_entity"][repo]
        if engine == "mongo":
            collection = model["entity_collection"].get(entity, entity[0].lower() + entity[1:])
        else:
            collection = model["entity_table"].get(entity)
        for mm in re.finditer(r'\b' + re.escape(var) + r'\s*\.\s*(\w+)\s*\(', body_text):
            rw = _mongo_op(mm.group(1))
            if rw:
                _emit(rw[0], rw[1], collection, f"{repo}.{mm.group(1)}", entity, engine)

    for var in template_fields:
        for mm in re.finditer(r'\b' + re.escape(var) + r'\s*\.\s*(\w+)\s*\(', body_text):
            rw = _mongo_op(mm.group(1))
            if not rw:
                continue
            open_idx = mm.end() - 1
            close_idx = find_matching(body_text, open_idx, "(", ")")
            args = split_top_level(body_text[open_idx + 1:close_idx - 1], ",")
            collection, entity = None, None
            for a in args:   # an `Entity.class` argument names the collection via @Document
                em = re.fullmatch(r'\s*(\w+)\.class\s*', a)
                if em and em.group(1) in model["entity_collection"]:
                    entity = em.group(1)
                    collection = model["entity_collection"][entity]
                    break
            if collection is None and args:
                # Only the LAST argument can be a collection name. Any other string
                # literal (e.g. Criteria.where("_id")) is a field name inside the query.
                sm = re.fullmatch(r'\s*"([A-Za-z0-9_.\-]+)"\s*', args[-1])
                if sm:
                    collection = sm.group(1)
            _emit(rw[0], rw[1], collection, f"MongoTemplate.{mm.group(1)}", entity)
        # A static helper handed the template (`Util.boundedFind(var, query, Entity.class)`): the
        # entity class still names the collection. Only calls whose name reads as a query/write are used.
        for hm in re.finditer(r'\b(\w+)\s*\.\s*(\w+)\s*\(\s*(?:this\s*\.\s*)?' + re.escape(var) + r'\s*[,)]', body_text):
            if hm.group(1) == var:
                continue
            open_idx = body_text.index("(", hm.end(2))
            close_idx = find_matching(body_text, open_idx, "(", ")")
            args = split_top_level(body_text[open_idx + 1:close_idx - 1], ",")
            entity = next((em.group(1) for em in (re.fullmatch(r'\s*(\w+)\.class\s*', a) for a in args)
                           if em and em.group(1) in model["entity_collection"]), None)
            rw = _mongo_op(hm.group(2)) or (("READS", "select") if _HELPER_READ_RE.search(hm.group(2)) else None)
            if entity and rw:
                _emit(rw[0], rw[1], model["entity_collection"][entity], f"{hm.group(1)}.{hm.group(2)}", entity)


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


def _parse_class_flows(db, registry, service_name, rel_file, full_text, cm, stats, mongo_model=None):
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
    mongo_ctx = None
    if mongo_model and cm.group("kind") == "class":
        repo_fields, template_fields = _mongo_fields(class_body_text, mongo_model)
        if repo_fields or template_fields:
            mongo_ctx = (repo_fields, template_fields, mongo_model)
    dep_fields = _dep_fields(class_body_text) if cm.group("kind") == "class" else {}
    if mongo_model:   # repositories are already modelled as READS/WRITES edges; a call edge would only bloat the graph
        dep_fields = {f: t for f, t in dep_fields.items() if t not in mongo_model["repo_entity"]}

    # Best-effort: the WebClient base URL, if this class builds one via
    # `builder.baseUrl("...")` — attributed to the whole class rather than a
    # specific field, so a class with >1 differently-based WebClient isn't
    # disambiguated (rare; see module docstring).
    webclient_base_url_match = WEBCLIENT_BASEURL_RE.search(class_body_text)
    webclient_prefix = (
        _HOST_STRIP_RE.sub("", webclient_base_url_match.group(1)).split(":", 1)[0].strip("/")
        if webclient_base_url_match else None
    )

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
                           class_node_id, field_qualifiers, webclient_prefix, stats, mongo_ctx, dep_fields)


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
                       class_node_id, field_qualifiers, webclient_prefix, stats, mongo_ctx=None, dep_fields=None):
    source_id = method_node_id if method_node_id is not None else class_node_id
    if source_id is None:
        return

    if dep_fields and method_node_id is not None:
        seen_calls = set()
        for dm in _DEP_CALL_RE.finditer(m.body_text):
            field, callee = dm.group(1), dm.group(2)
            dep_type = dep_fields.get(field)
            if not dep_type or callee in _DEP_CALL_SKIP or (dep_type, callee) in seen_calls:
                continue
            seen_calls.add((dep_type, callee))
            db.add_edge(source_id, "CALLS_DEP", target_name=f"{dep_type}#{callee}", service=service_name,
                        metadata=_meta("verified", field=field, type=dep_type, method=callee))
            stats["flow_edges"] = stats.get("flow_edges", 0) + 1
            stats["CALLS_DEP"] = stats.get("CALLS_DEP", 0) + 1

    if mongo_ctx:
        _scan_mongo(db, service_name, source_id, m.body_text, mongo_ctx[0], mongo_ctx[1], mongo_ctx[2], stats)

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
        # SEND_LITERAL_RE only ever matches a quoted string literal as the
        # topic argument (see its pattern) — the topic name is read
        # directly off the literal, not guessed from a dotted reference's
        # simple name (that's PUBLISH_CALL_RE below, which stays
        # "inferred"), so this is a "verified" fact per the confidence
        # model (an exact Kafka topic string found in source).
        topic_key = sm.group(3)
        topic_id = _get_or_create_topic(db, topic_key, service_name)
        db.add_edge(source_id, "PUBLISHES", target_id=topic_id, service=service_name,
                    metadata=_meta("verified", via=sm.group(2)))
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

    seen_sql = set()
    for text in _sql_candidate_texts(m.body_text):
        hits = [(relation, tm.group(1), op) for relation, pattern, op in SQL_TABLE_PATTERNS
                for tm in pattern.finditer(text) if tm.group(1) not in _PROSE_WORDS]
        hits += [("READS", table, "select") for table in _select_tables(text)]
        for relation, table, op in hits:
            if (relation, table) in seen_sql:
                continue
            seen_sql.add((relation, table))
            table_id = _get_or_create_table(db, table, service_name)
            db.add_edge(source_id, relation, target_id=table_id, service=service_name,
                        metadata=_meta("inferred", operation=op))
            stats["DB_TABLE"] = stats.get("DB_TABLE", 0) + 1
            stats["flow_edges"] = stats.get("flow_edges", 0) + 1

    if webclient_prefix:
        for wm in WEBCLIENT_CALL_RE.finditer(m.body_text):
            http_method = wm.group(1).upper()
            path = "/" + wm.group(3).lstrip("/")
            db.add_edge(
                source_id, "WEBCLIENT_CALL", target_name=f"webclient:{webclient_prefix}:{http_method}:{path}",
                service=service_name,
                metadata=_meta("unknown", target_prefix=webclient_prefix, http_method=http_method, path=path),
            )
            stats["flow_edges"] = stats.get("flow_edges", 0) + 1
            stats["WEBCLIENT_CALL"] = stats.get("WEBCLIENT_CALL", 0) + 1

    for rm in RESTTEMPLATE_CALL_RE.finditer(m.body_text):
        call_method = rm.group(1)
        host = rm.group(2)
        path = "/" + (rm.group(3) or "").lstrip("/")
        http_method = RESTTEMPLATE_METHOD_TO_HTTP.get(call_method)
        if http_method is None and call_method == "exchange":
            hm = RESTTEMPLATE_EXCHANGE_METHOD_RE.search(m.body_text[rm.end():rm.end() + 200])
            http_method = hm.group(1) if hm else None
        target_prefix = host.split(":", 1)[0].strip("/")
        db.add_edge(
            source_id, "RESTTEMPLATE_CALL",
            target_name=f"resttemplate:{target_prefix}:{http_method or '?'}:{path}",
            service=service_name,
            metadata=_meta("unknown", target_prefix=target_prefix, http_method=http_method, path=path),
        )
        stats["flow_edges"] = stats.get("flow_edges", 0) + 1
        stats["RESTTEMPLATE_CALL"] = stats.get("RESTTEMPLATE_CALL", 0) + 1


def parse_service(db, registry, service_name, service_path, progress=None, only_files=None):
    """Run after java_parser.parse_service() for the same service, before
    that service's resolve_pending_edges() call — see module docstring."""
    stats = {"flow_edges": 0}

    # Built from the whole service even under incremental only_files, since
    # an entity/repository in an unchanged file still resolves a changed caller.
    mongo_model = _build_mongo_model(service_path)
    if not mongo_model["repo_entity"] and not mongo_model["entity_collection"]:
        mongo_model = None

    for file_path in walk_source_files(service_path, (".java",), ignored_suffixes=("Test.java", "Tests.java")):
        if only_files is not None and file_path not in only_files:
            continue
        text = read_text(file_path)
        if text is None:
            continue
        rel_file = os.path.relpath(file_path, service_path)
        for cm in CLASS_DECL_RE.finditer(mask_java_comments(text)):
            try:
                _parse_class_flows(db, registry, service_name, rel_file, text, cm, stats, mongo_model)
            except Exception:
                stats["flow_errors"] = stats.get("flow_errors", 0) + 1
        if progress:
            progress(service_name, file_path, stats)

    return stats
