/*
 * pass_orm_nodes.cpp — Create Table nodes from JPA/Hibernate @Entity/@Table
 * annotations on Class nodes. Also creates MongoCollection/MongoField nodes
 * from Spring Data MongoDB @Document/@Field annotations (see
 * cbm_pipeline_create_mongo_nodes below) — same file, since both share the
 * same decorator-extraction helpers; kept as two distinct exported passes,
 * not merged into one function, so SQL and Mongo persistence stay separable
 * in the pipeline registration (see pipeline.cpp).
 *
 * Additive only: every node already carries its raw decorator strings in
 * properties_json's "decorators" array (see pass_definitions.cpp's
 * append_json_str_array call, and cbm.h's CBMDefinition.decorators). This
 * pass reads that existing data on Class nodes — it doesn't touch parsing,
 * doesn't modify any existing pass, and doesn't run unless a class actually
 * carries @Entity.
 *
 * A class needs @Entity present to count as a JPA entity at all — @Table
 * alone with no @Entity isn't a persistent type per the JPA spec. Table
 * name: @Table(name="...") if present; otherwise the entity's own simple
 * class name, which is JPA/Hibernate's own documented default when @Table
 * is omitted — a specification-defined rule, not a naming-convention guess.
 *
 * Field-level @Column mapping is deliberately NOT attempted here: this
 * codebase has no Field node type to attach it to (confirmed — Class,
 * Function, Method, Interface, Enum, Route, Resource, ... but no Field),
 * and adding one is a schema change, not a same-shaped additive pass like
 * this one. Left as a follow-up once that primitive exists.
 *
 * Strict two-phase execution, deliberately: cbm_gbuf_find_by_label (see
 * graph_buffer.cpp) returns a live pointer into the buffer's own per-label
 * index array, not a snapshot. Calling cbm_gbuf_upsert_node/insert_edge
 * *while* iterating that same array crashed in testing — plausibly the
 * "Table" label's own bucket growing during iteration, since "Table"
 * already has ~39 entries from SQL-schema parsing before this pass ever
 * runs. Phase 1 reads only (classes[], decorators) and writes zero nodes;
 * phase 2 iterates a plain local std::vector built in phase 1, so nothing
 * touches the live gbuf arrays while they're being read.
 */
#include "pipeline/pipeline_internal.h"
#include "graph_buffer/graph_buffer.h"
#include "foundation/log.h"
#include "foundation/compat_fs.h"
#include "foundation/limits.h"
#include "yyjson/yyjson.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <ctype.h>
#include <string>
#include <vector>
#include <algorithm>

namespace {

char **extract_decorators_from_json(const char *json) {
    if (!json) return NULL;
    yyjson_doc *doc = yyjson_read(json, strlen(json), 0);
    if (!doc) return NULL;

    yyjson_val *root = yyjson_doc_get_root(doc);
    yyjson_val *decs = yyjson_obj_get(root, "decorators");
    if (!decs || !yyjson_is_arr(decs)) {
        yyjson_doc_free(doc);
        return NULL;
    }
    size_t cnt = yyjson_arr_size(decs);
    if (cnt == 0) {
        yyjson_doc_free(doc);
        return NULL;
    }

    char **out = (char **)calloc(cnt + 1, sizeof(char *));
    size_t idx = 0;
    yyjson_val *item;
    yyjson_arr_iter iter;
    yyjson_arr_iter_init(decs, &iter);
    while ((item = yyjson_arr_iter_next(&iter))) {
        if (yyjson_is_str(item)) {
            out[idx++] = strdup(yyjson_get_str(item));
        }
    }
    out[idx] = NULL;
    yyjson_doc_free(doc);
    if (idx > 0) return out;
    free(out);
    return NULL;
}

void free_decorators(char **decorators) {
    if (!decorators) return;
    for (int i = 0; decorators[i]; i++) free(decorators[i]);
    free(decorators);
}

/* Decorator strings retain their leading "@" (confirmed against the actual
 * persisted graph — e.g. "@Entity", "@DiscriminatorValue(\"BED-CAT\")"), so
 * `tag` is matched starting at decorators[i]+1, not +0. Matched from the
 * start so "EntityListeners" never matches tag "Entity". */
bool has_decorator_named(char **decorators, const char *tag) {
    if (!decorators) return false;
    size_t tag_len = strlen(tag);
    for (int i = 0; decorators[i]; i++) {
        const char *d = decorators[i];
        if (d[0] != '@') continue;
        d++;
        if (strncmp(d, tag, tag_len) == 0 && (d[tag_len] == '\0' || d[tag_len] == '(')) {
            return true;
        }
    }
    return false;
}

/* Extract a quoted string argument, e.g. name="foo" or name = "foo", from a
 * decorator matching `@tag(...)`. Returns a malloc'd copy, or NULL if the
 * decorator/tag/arg isn't present. */
char *find_decorator_string_arg(char **decorators, const char *tag, const char *arg_name) {
    if (!decorators) return NULL;
    size_t tag_len = strlen(tag);
    for (int i = 0; decorators[i]; i++) {
        const char *d = decorators[i];
        if (d[0] != '@') continue;
        d++;
        if (strncmp(d, tag, tag_len) != 0 || d[tag_len] != '(') continue;
        const char *arg_pos = strstr(d, arg_name);
        if (!arg_pos) continue;
        const char *eq = strchr(arg_pos, '=');
        if (!eq) continue;
        const char *q1 = strchr(eq, '"');
        if (!q1) continue;
        const char *q2 = strchr(q1 + 1, '"');
        if (!q2) continue;
        size_t len = (size_t)(q2 - q1 - 1);
        char *out = (char *)malloc(len + 1);
        memcpy(out, q1 + 1, len);
        out[len] = '\0';
        return out;
    }
    return NULL;
}

/* Extract a *bare* positional string argument, e.g. @Document("foo") — no
 * "key =" before the quote. Deliberately does NOT match @Document(collection
 * = "foo"): the first non-space char after '(' must be '"' itself, not a
 * bareword key, so this never double-counts a named arg as if it were bare. */
char *find_decorator_bare_string_arg(char **decorators, const char *tag) {
    if (!decorators) return NULL;
    size_t tag_len = strlen(tag);
    for (int i = 0; decorators[i]; i++) {
        const char *d = decorators[i];
        if (d[0] != '@') continue;
        d++;
        if (strncmp(d, tag, tag_len) != 0 || d[tag_len] != '(') continue;
        const char *p = d + tag_len + 1;
        while (*p == ' ' || *p == '\t' || *p == '\n') p++;
        if (*p != '"') continue;
        const char *q2 = strchr(p + 1, '"');
        if (!q2) continue;
        size_t len = (size_t)(q2 - p - 1);
        char *out = (char *)malloc(len + 1);
        memcpy(out, p + 1, len);
        out[len] = '\0';
        return out;
    }
    return NULL;
}

/* Read a single string property (e.g. "parent_class") out of a node's
 * properties_json. Returns a malloc'd copy, or NULL. */
char *extract_string_prop(const char *json, const char *prop) {
    if (!json) return NULL;
    yyjson_doc *doc = yyjson_read(json, strlen(json), 0);
    if (!doc) return NULL;
    yyjson_val *root = yyjson_doc_get_root(doc);
    yyjson_val *val = yyjson_obj_get(root, prop);
    char *out = NULL;
    if (val && yyjson_is_str(val)) {
        out = strdup(yyjson_get_str(val));
    }
    yyjson_doc_free(doc);
    return out;
}

/* Read the whole file at an absolute path into a heap buffer (NUL-terminated).
 * Caller frees. Bounded by cbm_max_file_bytes() same as the rest of the
 * pipeline — this is only ever called for the small set of files already
 * confirmed to contain an @Document class, never a blanket repo scan. */
char *read_whole_file(const char *abs_path) {
    FILE *f = cbm_fopen(abs_path, "rb");
    if (!f) return NULL;
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return NULL; }
    long size = ftell(f);
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return NULL; }
    if (size <= 0 || size > cbm_max_file_bytes()) { fclose(f); return NULL; }
    char *buf = (char *)malloc((size_t)size + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t n = fread(buf, 1, (size_t)size, f);
    fclose(f);
    buf[n] = '\0';
    return buf;
}

/* Read [start_line,end_line] (1-based, inclusive) of a file back as a single
 * string, or "" if the file/range can't be read. Shared by the @Field scan
 * and the MongoRepository<Entity,ID> generic-argument scan below — both need
 * the same "resolve relative file_path against repo root, slice by line
 * range" step. */
std::string read_body_lines(const std::string &repo_path, const char *rel_file_path,
                             int start_line, int end_line) {
    if (repo_path.empty() || !rel_file_path || end_line < start_line || start_line <= 0) return "";
    std::string abs_path = repo_path + "/" + rel_file_path;
    char *whole = read_whole_file(abs_path.c_str());
    if (!whole) return "";
    std::string text(whole);
    free(whole);
    std::vector<std::string> lines;
    size_t start = 0;
    for (size_t p = 0; p <= text.size(); p++) {
        if (p == text.size() || text[p] == '\n') {
            lines.push_back(text.substr(start, p - start));
            start = p + 1;
        }
    }
    int from = start_line - 1;
    int to = std::min((int)lines.size(), end_line);
    std::string body;
    for (int ln = from; ln < to; ln++) {
        body += lines[(size_t)ln];
        body += '\n';
    }
    return body;
}

struct EntityMapping {
    int64_t class_id;
    std::string table_name;
    std::string file_path;
    bool explicit_name;
};

struct MongoEntityMapping {
    int64_t class_id;
    std::string class_qn;
    std::string collection_name;
    std::string file_path;
    std::string confidence; /* "verified" | "verified_default" */
};

struct MongoFieldMapping {
    int64_t field_node_id; /* the real, existing Field node — 0 if no match found */
    std::string collection_name;
    std::string java_field_name;
    std::string mongo_field_name;
    std::string file_path;
    int line;
};

struct MongoRepoMapping {
    int64_t repo_id;
    std::string entity_simple_name;
};

struct FieldAnnotationHit {
    std::string java_field_name;
    std::string mongo_field_name;
    int line_offset; /* 0-based newline count from start of the scanned body */
};

bool is_ident_char(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
}

/* Scan a class body's raw text (already scoped to [start_line,end_line], not
 * the whole file) for @Field(...) / @Field("...") / bare @Field annotations,
 * returning the effective mongo field name for each. Field-level scanning is
 * the one place this pass reads source text directly rather than reusing
 * already-extracted decorator data — Code Cortex's Field nodes don't carry
 * decorators (confirmed via get_graph_schema: Field's properties list has no
 * "decorators" key, unlike Class/Method/Function/Interface), and it's scoped
 * to only the already-confirmed-@Document class bodies, never a blanket
 * repo-wide text scan. */
std::vector<FieldAnnotationHit> scan_field_annotations(const std::string &body) {
    std::vector<FieldAnnotationHit> hits;
    size_t pos = 0;
    while ((pos = body.find("@Field", pos)) != std::string::npos) {
        size_t after_tag = pos + 6; /* strlen("@Field") */
        char next = after_tag < body.size() ? body[after_tag] : '\0';
        if (is_ident_char(next)) {
            /* e.g. @FieldDefaults — not @Field */
            pos = after_tag;
            continue;
        }
        int line_offset = (int)std::count(body.begin(), body.begin() + (long)pos, '\n');

        std::string mongo_name;
        size_t scan_end = after_tag;
        if (next == '(') {
            size_t close = body.find(')', after_tag);
            if (close != std::string::npos) {
                std::string args = body.substr(after_tag + 1, close - after_tag - 1);
                size_t name_kw = args.find("name");
                size_t q1;
                if (name_kw != std::string::npos && args.find('=', name_kw) != std::string::npos) {
                    q1 = args.find('"', args.find('=', name_kw));
                } else {
                    q1 = args.find('"');
                }
                if (q1 != std::string::npos) {
                    size_t q2 = args.find('"', q1 + 1);
                    if (q2 != std::string::npos) {
                        mongo_name = args.substr(q1 + 1, q2 - q1 - 1);
                    }
                }
                scan_end = close + 1;
            }
        }

        /* Find the field's Java name: skip modifiers/whitespace/other
         * annotations up to ';' or '=', take the last identifier token
         * before it (handles "private String x;", "private List<String>
         * tags;"). Deliberately simple — this is a scoped heuristic over
         * an already-narrow body, not a general Java parser. */
        size_t stmt_end = body.find_first_of(";=", scan_end);
        if (stmt_end != std::string::npos && !mongo_name.empty()) {
            size_t j = stmt_end;
            while (j > scan_end && !is_ident_char(body[j - 1])) j--;
            size_t ident_end = j;
            while (j > scan_end && is_ident_char(body[j - 1])) j--;
            std::string java_field = body.substr(j, ident_end - j);
            if (!java_field.empty()) {
                FieldAnnotationHit hit;
                hit.java_field_name = java_field;
                hit.mongo_field_name = mongo_name;
                hit.line_offset = line_offset;
                hits.push_back(hit);
            }
        }
        pos = scan_end;
    }
    return hits;
}

} // namespace

void cbm_pipeline_create_orm_nodes(cbm_gbuf_t *gb) {
    const cbm_gbuf_node_t **classes = NULL;
    int class_count = 0;
    if (cbm_gbuf_find_by_label(gb, "Class", &classes, &class_count) != 0 || class_count == 0) {
        return;
    }

    /* Phase 1: read-only. Nothing here calls upsert_node/insert_edge, so the
     * `classes` array (live view into the gbuf) is never mutated while it's
     * being walked. */
    std::vector<EntityMapping> mappings;
    mappings.reserve(16);
    for (int i = 0; i < class_count; i++) {
        const cbm_gbuf_node_t *cls = classes[i];
        char **decorators = extract_decorators_from_json(cls->properties_json);
        if (!decorators) continue;

        if (has_decorator_named(decorators, "Entity")) {
            char *explicit_name = find_decorator_string_arg(decorators, "Table", "name");
            EntityMapping m;
            m.class_id = cls->id;
            m.table_name = explicit_name ? explicit_name : (cls->name ? cls->name : "");
            m.file_path = cls->file_path ? cls->file_path : "";
            m.explicit_name = explicit_name != NULL;
            if (!m.table_name.empty()) {
                mappings.push_back(m);
            }
            free(explicit_name);
        }
        free_decorators(decorators);
    }

    /* Phase 2: writes only, iterating our own local vector — the live gbuf
     * arrays are never touched mid-read again from here on. */
    int entities_found = 0;
    for (const EntityMapping &m : mappings) {
        std::string table_qn = "table:" + m.table_name;
        std::string props = std::string("{\"confidence\":\"") +
                             (m.explicit_name ? "verified" : "verified_default") +
                             "\",\"source\":\"@Entity/@Table\"}";

        int64_t table_id = cbm_gbuf_upsert_node(gb, "Table", m.table_name.c_str(), table_qn.c_str(),
                                                 m.file_path.c_str(), 0, 0, props.c_str());
        if (table_id > 0) {
            cbm_gbuf_insert_edge(gb, m.class_id, table_id, "MAPS_TO_TABLE", props.c_str());
            entities_found++;
        }
    }

    if (entities_found > 0) {
        /* cbm_log's variadic args are (key, value) C-string pairs, NULL-terminated
         * — NOT printf-style (see foundation/log.h's documented contract). Passing
         * an int directly here, as an earlier version of this function did, makes
         * cbm_log read that int as a key pointer and segfault; every call must go
         * through a real string like this snprintf. */
        char count_buf[16];
        snprintf(count_buf, sizeof(count_buf), "%d", entities_found);
        cbm_log_info("orm_nodes.done", "entities_mapped", count_buf);
    }
}

/* @Document -> MongoCollection, @Field -> MongoField, MongoRepository<X,_> -> X.
 *
 * Same two-phase discipline as cbm_pipeline_create_orm_nodes above, for the
 * same reason (mutating a live find_by_label array mid-iteration crashed
 * in testing on this exact codebase — see that function's header comment).
 * Phase 1 does all reads, including the one piece of real file I/O this
 * pass needs (see scan_field_annotations's doc comment for why); phase 2
 * only ever touches its own local vectors.
 *
 * MongoCollection/MongoField are distinct node labels from Table/Column —
 * SQL and Mongo persistence are never merged into one node type, so a
 * trace can always tell which kind of store it's looking at. */
void cbm_pipeline_create_mongo_nodes(cbm_pipeline_ctx_t *ctx) {
    cbm_gbuf_t *gb = ctx->gbuf;
    std::string repo_path = ctx->repo_path ? ctx->repo_path : "";

    const cbm_gbuf_node_t **classes = NULL;
    int class_count = 0;
    cbm_gbuf_find_by_label(gb, "Class", &classes, &class_count);

    const cbm_gbuf_node_t **interfaces = NULL;
    int interface_count = 0;
    cbm_gbuf_find_by_label(gb, "Interface", &interfaces, &interface_count);

    /* ---- Phase 1: read-only ---- */
    std::vector<MongoEntityMapping> entity_mappings;
    std::vector<MongoFieldMapping> field_mappings;
    std::vector<MongoRepoMapping> repo_mappings;

    for (int i = 0; i < class_count; i++) {
        const cbm_gbuf_node_t *cls = classes[i];
        char **decorators = extract_decorators_from_json(cls->properties_json);
        if (!decorators) continue;

        if (has_decorator_named(decorators, "Document")) {
            char *collection = find_decorator_string_arg(decorators, "Document", "collection");
            std::string confidence = "verified";
            if (!collection) {
                collection = find_decorator_string_arg(decorators, "Document", "value");
            }
            if (!collection) {
                collection = find_decorator_bare_string_arg(decorators, "Document");
            }
            std::string collection_name;
            if (collection) {
                collection_name = collection;
            } else if (cls->name && cls->name[0]) {
                /* Spring Data MongoDB's documented default when @Document has no
                 * collection/value: the class's simple name with only the first
                 * character lowercased (NOT snake_case) — verified against
                 * Spring's own docs before implementing, not assumed. Framework
                 * spec, not a project convention guess, so "verified_default"
                 * (same tier as the JPA @Table-omitted default above), never
                 * plain "verified" since no annotation literally states it. */
                collection_name = cls->name;
                collection_name[0] = (char)tolower((unsigned char)collection_name[0]);
                confidence = "verified_default";
            }
            free(collection);

            if (!collection_name.empty()) {
                MongoEntityMapping em;
                em.class_id = cls->id;
                em.class_qn = cls->qualified_name ? cls->qualified_name : "";
                em.collection_name = collection_name;
                em.file_path = cls->file_path ? cls->file_path : "";
                em.confidence = confidence;
                entity_mappings.push_back(em);

                /* Field-level scan, scoped to this one already-confirmed
                 * @Document class's own line range — see scan_field_annotations
                 * doc comment for why this needs a file read at all. */
                std::string body =
                    read_body_lines(repo_path, cls->file_path, cls->start_line, cls->end_line);
                for (const FieldAnnotationHit &hit : scan_field_annotations(body)) {
                    MongoFieldMapping fm;
                    fm.field_node_id = 0;
                    fm.collection_name = collection_name;
                    fm.java_field_name = hit.java_field_name;
                    fm.mongo_field_name = hit.mongo_field_name;
                    fm.file_path = cls->file_path;
                    fm.line = cls->start_line + hit.line_offset;

                    /* Resolve to the REAL, already-indexed Field node (Code
                     * Cortex already has 25,921 of these — see
                     * get_graph_schema) so the edge starts from actual
                     * indexed data, not a synthetic stand-in. */
                    const cbm_gbuf_node_t **candidates = NULL;
                    int cand_count = 0;
                    cbm_gbuf_find_by_name(gb, hit.java_field_name.c_str(), &candidates,
                                           &cand_count);
                    for (int c = 0; c < cand_count; c++) {
                        if (!candidates[c]->label || strcmp(candidates[c]->label, "Field") != 0) {
                            continue;
                        }
                        char *parent =
                            extract_string_prop(candidates[c]->properties_json, "parent_class");
                        bool match = parent && em.class_qn == parent;
                        free(parent);
                        if (match) {
                            fm.field_node_id = candidates[c]->id;
                            break;
                        }
                    }
                    field_mappings.push_back(fm);
                }
            }
        }
        free_decorators(decorators);
    }

    for (int i = 0; i < interface_count; i++) {
        const cbm_gbuf_node_t *iface = interfaces[i];
        char **iface_decorators = extract_decorators_from_json(iface->properties_json);
        /* base_classes on the persisted node has its generic args already
         * stripped by the extraction layer (confirmed empirically: an
         * interface declared "extends MongoRepository<PatientDetails,
         * String>" persists as base_classes:["MongoRepository"], no <...> —
         * this property alone can't give us the entity type). Use it only
         * to confirm this interface is Mongo-repository-shaped at all,
         * cheaply, before paying for a file read. */
        char *base_classes_json = iface->properties_json ? strdup(iface->properties_json) : NULL;
        bool looks_like_mongo_repo = false;
        if (base_classes_json) {
            yyjson_doc *doc = yyjson_read(base_classes_json, strlen(base_classes_json), 0);
            if (doc) {
                yyjson_val *root = yyjson_doc_get_root(doc);
                yyjson_val *bcs = yyjson_obj_get(root, "base_classes");
                if (bcs && yyjson_is_arr(bcs)) {
                    yyjson_val *item;
                    yyjson_arr_iter iter;
                    yyjson_arr_iter_init(bcs, &iter);
                    while ((item = yyjson_arr_iter_next(&iter))) {
                        if (yyjson_is_str(item) && strstr(yyjson_get_str(item), "MongoRepository")) {
                            looks_like_mongo_repo = true;
                        }
                    }
                }
                yyjson_doc_free(doc);
            }
            free(base_classes_json);
        }
        free_decorators(iface_decorators);
        if (!looks_like_mongo_repo) continue;

        /* Get the actual generic argument from raw source — scoped to just
         * this interface's own declaration range, same pattern as the
         * @Field scan above. */
        std::string body =
            read_body_lines(repo_path, iface->file_path, iface->start_line, iface->end_line);
        size_t marker = body.find("MongoRepository");
        if (marker == std::string::npos) continue;
        size_t lt = body.find('<', marker);
        if (lt == std::string::npos) continue;
        size_t comma = body.find(',', lt);
        size_t gt = body.find('>', lt);
        size_t stop = comma;
        if (stop == std::string::npos || (gt != std::string::npos && gt < comma)) stop = gt;
        if (stop == std::string::npos || stop <= lt + 1) continue;
        std::string entity_name = body.substr(lt + 1, stop - lt - 1);
        size_t b = entity_name.find_first_not_of(" \t");
        size_t e = entity_name.find_last_not_of(" \t");
        if (b == std::string::npos) continue;
        entity_name = entity_name.substr(b, e - b + 1);

        MongoRepoMapping rm;
        rm.repo_id = iface->id;
        rm.entity_simple_name = entity_name;
        repo_mappings.push_back(rm);
    }

    /* ---- Phase 2: writes only, over our own local vectors ---- */
    int collections_found = 0;
    int fields_found = 0;
    int repos_found = 0;

    for (const MongoEntityMapping &em : entity_mappings) {
        std::string coll_qn = "mongo_collection:" + em.collection_name;
        std::string props = std::string("{\"confidence\":\"") + em.confidence +
                             "\",\"source\":\"@Document\"}";
        int64_t coll_id = cbm_gbuf_upsert_node(gb, "MongoCollection", em.collection_name.c_str(),
                                                coll_qn.c_str(), em.file_path.c_str(), 0, 0,
                                                props.c_str());
        if (coll_id > 0) {
            cbm_gbuf_insert_edge(gb, em.class_id, coll_id, "MAPS_TO_MONGO_COLLECTION", props.c_str());
            collections_found++;
        }
    }

    for (const MongoFieldMapping &fm : field_mappings) {
        std::string field_qn = "mongo_field:" + fm.collection_name + "." + fm.mongo_field_name;
        std::string props = "{\"confidence\":\"verified\",\"source\":\"@Field\"}";
        int64_t mf_id = cbm_gbuf_upsert_node(gb, "MongoField", fm.mongo_field_name.c_str(),
                                              field_qn.c_str(), fm.file_path.c_str(), fm.line,
                                              fm.line, props.c_str());
        if (mf_id > 0 && fm.field_node_id > 0) {
            cbm_gbuf_insert_edge(gb, fm.field_node_id, mf_id, "MAPS_TO_MONGO_FIELD", props.c_str());
            fields_found++;
        }
    }

    for (const MongoRepoMapping &rm : repo_mappings) {
        const cbm_gbuf_node_t **candidates = NULL;
        int cand_count = 0;
        cbm_gbuf_find_by_name(gb, rm.entity_simple_name.c_str(), &candidates, &cand_count);
        for (int c = 0; c < cand_count; c++) {
            if (!candidates[c]->label || strcmp(candidates[c]->label, "Class") != 0) continue;
            std::string props = "{\"confidence\":\"verified\",\"source\":\"MongoRepository<T,ID>\"}";
            cbm_gbuf_insert_edge(gb, rm.repo_id, candidates[c]->id, "REPOSITORY_FOR", props.c_str());
            repos_found++;
            break;
        }
    }

    if (collections_found > 0 || fields_found > 0 || repos_found > 0) {
        char coll_buf[16], field_buf[16], repo_buf[16];
        snprintf(coll_buf, sizeof(coll_buf), "%d", collections_found);
        snprintf(field_buf, sizeof(field_buf), "%d", fields_found);
        snprintf(repo_buf, sizeof(repo_buf), "%d", repos_found);
        cbm_log_info("mongo_nodes.done", "collections", coll_buf, "fields", field_buf, "repos",
                     repo_buf);
    }
}
