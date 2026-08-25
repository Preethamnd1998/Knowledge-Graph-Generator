# Knowledge Graph MCP Server

An MCP (Model Context Protocol) server that sits between the knowledge
graph `indexer.py` produces and an AI agent, so the agent never needs to
know the graph's SQL schema, node types, or edge relations — it calls
semantic tools like `find_service` or `get_business_flow` and gets back
a small, structured, ready-to-use answer.

```text
Customer Source Repositories
        |
Knowledge Graph Indexer          (indexer.py, kg_indexer/)
        |
Knowledge Graph DB                (output/knowledge_graph.db)
        |
Knowledge Graph MCP Server        (mcp_server/)  <-- this document
        |
AI Agents
        |
Claude / Coding Agents / Enterprise AI Platform
```

## 1. Architecture

```text
mcp_server/
├── server.py           entry point — builds the MCPServer, registers all tools, runs stdio
├── config.py            reads KG_DB_PATH / MCP_* from the environment
├── db.py                 read-only SQLite connection (mode=ro) + query-timeout guard
├── errors.py              exception types + the "never leak a raw message" boundary
├── limits.py              depth/limit clamping shared by every tool
├── logging_setup.py       per-tool call logging + the try/except error boundary
│
├── models/
│   ├── context.py         response-shaping helpers, token-count estimate
│   └── evidence.py         confidence levels (verified/inferred/unknown), prune_empty()
│
├── queries/                SQL + graph-traversal logic — no MCP/agent concepts in here
│   ├── common.py            node/edge lookups, Controller/Service/Repository classification,
│   │                        the shared class_dependency_chain() and traverse() BFS
│   ├── service_queries.py
│   ├── api_queries.py
│   ├── flow_queries.py
│   ├── database_queries.py
│   ├── dependency_queries.py
│   └── search_queries.py
│
└── tools/                  one @server.tool()-registered function per tool, thin:
    ├── service_tools.py      validate input -> call queries/* -> return
    ├── api_tools.py
    ├── flow_tools.py
    ├── database_tools.py
    ├── dependency_tools.py
    ├── search_tools.py
    ├── evidence_tools.py
    └── context_pack_tools.py
```

**Why this split.** `queries/` never imports anything from `mcp` or
`tools/` — it's plain SQLite + Python, testable and importable with zero
MCP SDK dependency (see "Testing" below). `tools/` is the only layer that
knows about MCP: input validation, depth/limit clamping, and turning a
query result into the exact compact shape a tool promises. If you ever
add an HTTP/SSE transport, or swap the MCP SDK, only `server.py` and the
`register()` calls at the bottom of each `tools/*.py` file change.

### The graph schema this server was built against

Derived entirely from `kg_indexer/db.py` (SCHEMA) — this server does
**not** invent schema; if you change the indexer's schema, update the
query layer to match, not the other way around.

Two tables:

- **`nodes`**: `id, node_type, name, qualified_name, service, project_type, file_path, start_line, end_line, signature, metadata (JSON)`
  `node_type` values: `SERVICE, FILE, CLASS, INTERFACE, ENUM, METHOD, ENDPOINT` (Java) ·
  `NG_SUBPROJECT, NG_COMPONENT, NG_MODULE, NG_SERVICE, NG_DIRECTIVE, NG_PIPE` (Angular) ·
  `REACT_COMPONENT, REACT_HOOK` (React) · `API_CONSTANT` (Angular/React UI
  API constant field — see AGENTS.md "UI -> Backend API mapping").
- **`edges`**: `id, source_id, target_id (NULL if unresolved), target_name, relation, service, metadata (JSON)`
  `relation` values: `CONTAINS, DECLARES, EXTENDS, IMPLEMENTS, DEPENDS_ON, EXPOSES, IMPORTS, EXPORTS, USES_HOOK, RENDERS`,
  plus `CALLS_API` (UI API_CONSTANT -> backend ENDPOINT), `USES_API_CONSTANT`
  (UI file -> API_CONSTANT), `FEIGN_CALL`/`WEBCLIENT_CALL`/`RESTTEMPLATE_CALL`
  (backend METHOD -> another backend service's ENDPOINT) — all four require
  the graph to have been built with `SINGLE_DATABASE=true`.

There is **no** `CALLS` relation (no method-body call-site tracking) and
**no** `DATABASE_TABLE`/`BUSINESS_FLOW` node type. Several tools
(`get_business_flow`, `get_database_impact`, `find_callers`) answer
questions the schema doesn't directly model — see "Honesty model" below
for exactly how, and where that's `inferred` rather than `verified`.

## 2. Installation

The indexer (`indexer.py`, `kg_indexer/`) has **zero** dependencies and
is unaffected by any of this. The MCP server needs the `mcp` SDK, which
pulls in a fairly large dependency tree (starlette, pydantic, httpx,
cryptography, ...) — **install it into its own virtual environment**,
never into a shared/global Python install (those transitive versions
can collide with other tools on your machine — this is not
hypothetical, see "Lessons learned" below).

```powershell
cd "knowledge-graph-generator"
python -m venv .venv-mcp
.venv-mcp\Scripts\activate
pip install -r requirements-mcp.txt
```

(`.venv-mcp/` is gitignored — every developer creates their own.)

## 3. Configuration

Copy `mcp-config.example.env` for reference — in practice you'll set
these as the `env` block of your MCP client config (see §5), since the
client spawns the server process with whatever environment you give it.

| Variable | Default | Meaning |
|---|---|---|
| `KG_DB_PATH` | `./output/knowledge_graph.db` | The graph to serve. Relative paths resolve against the repo root regardless of the process's cwd. |
| `MCP_SERVER_NAME` | `knowledge-graph` | Name advertised to MCP clients. |
| `MCP_LOG_LEVEL` | `INFO` | `DEBUG\|INFO\|WARNING\|ERROR` |
| `MCP_MAX_RESULTS` | `10` | Ceiling for every list a tool can return. A caller may ask for fewer; more is silently capped. |
| `MCP_MAX_DEPTH` | `3` | Ceiling for traversal depth. Default depth when unspecified is always `1`. |
| `MCP_MAX_CONTEXT_TOKENS` | `3000` | Fallback budget `get_context_pack` trims itself to fit. |
| `MCP_QUERY_TIMEOUT_SECONDS` | `5` | A single SQLite query running longer than this is aborted (`query_timeout` error). |

**Requires `SINGLE_DATABASE=true`** in `kg-config.env`. Dependency and
caller lookups cross service boundaries by design (that's most of the
value); a per-service `.db` file literally cannot represent a
cross-service edge, since the other service's nodes live in a different
file. If you only have per-service `.db` files, re-run `python
indexer.py` with `SINGLE_DATABASE=true`.

## 4. Running the server

```powershell
.venv-mcp\Scripts\python.exe mcp_server_main.py
```

**Always invoke `mcp_server_main.py` (the repo-root bootstrap script),
not `mcp_server/server.py` and not `python -m mcp_server.server`.**
`server.py` uses relative imports (`from .config import ...`) because
it's part of the `mcp_server` package, so:

- `python mcp_server/server.py` fails outright — loaded as `__main__`
  with no parent package, it can't resolve `from .config import ...`
  (`ImportError: attempted relative import with no known parent package`).
- `python -m mcp_server.server` *works, but only if the process's
  current working directory is already the repo root* — different MCP
  clients default to different cwds when spawning a stdio server.
  Claude Code happened to use the repo root and this worked; VS Code's
  MCP client uses the workspace root **one level up**, and the exact
  same command failed there with
  `ModuleNotFoundError: No module named 'mcp_server'`.

`mcp_server_main.py` fixes this at the source: it inserts its own
directory into `sys.path` before importing anything, so it works
identically regardless of which directory the calling process happens
to be in. Use it — and its absolute path — in every client config
below rather than relying on any client's cwd default.

It serves over **stdio** by default — nothing to "start and leave
running" yourself; an MCP client spawns it per-session. Manually running
it will just sit there waiting for JSON-RPC on stdin, which is correct.

HTTP/SSE is available from the same `MCPServer` instance
(`server.run("streamable-http")` in `server.py`'s `main()`) without
touching anything in `tools/` or `queries/` — nothing in this codebase
assumes stdio beyond that one call.

### Running in Docker

`docker-compose.yml` has a `knowledge-graph-mcp` service (built from
`Dockerfile.mcp`) alongside the indexer's `kg-indexer` service. It mounts
`./output:/app/output:ro` — read-only, so a bug in the container can't
touch the file the indexer wrote.

`docker compose up` won't work for it — stdio needs something attached
to the container's stdin/stdout, which `up` doesn't provide. Either run
it the way an MCP client would:

```
docker compose run --rm -T knowledge-graph-mcp
```

or point an MCP client's `command`/`args` at `docker` directly instead
of at the venv's Python:

```json
{
  "command": "docker",
  "args": ["compose", "run", "--rm", "-T", "knowledge-graph-mcp"]
}
```

## 5. Claude Code configuration

**Verified against this repo's actual `mcp` SDK version (2.0.0) and
Claude Code CLI** — not assumed from older documentation:

```powershell
claude mcp add knowledge-graph -s local `
  -e KG_DB_PATH=./output/knowledge_graph.db `
  -- "<repo>\.venv-mcp\Scripts\python.exe" "<repo>\mcp_server_main.py"
```

Use absolute paths for both the interpreter and the script — `claude mcp
add`'s cwd when it writes the config isn't necessarily the cwd Claude
Code will use when it *spawns* the server later, and `mcp_server_main.py`
being cwd-independent only fixes the Python-import half of that; the
OS still needs an absolute or correctly-relative path to find the file
in the first place.

`claude mcp get knowledge-graph` should report `Status: Connected`. This
writes to your local (private, per-project) Claude Code config. Remove
with `claude mcp remove knowledge-graph -s local`.

Equivalent hand-edited JSON (e.g. a project-shared `.mcp.json`, or
Claude Desktop's config) — the `mcpServers` shape is a **client**
convention, not something the `mcp` SDK version dictates, but the
command/args below are specific to this server:

```json
{
  "mcpServers": {
    "knowledge-graph": {
      "command": "<repo>\\.venv-mcp\\Scripts\\python.exe",
      "args": ["<repo>\\mcp_server_main.py"],
      "env": {
        "KG_DB_PATH": "<repo>\\output\\knowledge_graph.db"
      },
      "type": "stdio"
    }
  }
}
```

This exact shape (absolute paths throughout) is what's checked into
this workspace's shared `D:\GitLab-Workspace\.mcp.json` — VS Code's
built-in MCP client reads that same file, which is how the cwd bug
above was actually found: VS Code spawns from the workspace root, one
directory above this repo.

On macOS/Linux, `command` is `<repo>/.venv-mcp/bin/python`.

## 6. Available tools

| Tool | Purpose |
|---|---|
| `find_service` | Find services/sub-projects by name or keyword |
| `get_service_context` | Compact overview: purpose, tech, counts, cross-service deps, events |
| `get_service_dependencies` | Service-level dependency tree, bounded depth |
| `find_api` | Find REST APIs by keyword/path/method/service |
| `get_api_context` | Full detail on one API: controller, DTOs, repos, tables, downstream services, UI callers |
| `resolve_ui_api` | Which backend endpoint(s) a UI API constant (`Foo.FIELD` / literal path) calls |
| `get_business_flow` | UI callers -> controller -> service -> repository -> downstream call chain for a named flow |
| `trace_business_flow` | Multi-hop, confidence-tagged flow from ANY named node (API constant, component, class, method, endpoint, Kafka topic, DB table, Batch Job/Step/Tasklet) |
| `get_database_impact` | Inferred tables read/written/updated/deleted for a service/entity/API/flow |
| `find_callers` | What depends on / holds a reference to a target |
| `find_dependencies` | Generic outgoing/incoming dependency lookup for any target |
| `search_knowledge` | General-purpose entry point; ranks flows above isolated nodes |
| `get_source_evidence` | File/line/signature pointer for one node |
| `get_context_pack` | Compressed Markdown package for a topic, budgeted to `max_tokens` |

## 7. Tool parameters

**`find_service(query, limit=None)`** — `query` required.

**`get_service_context(service)`** — `service` required, exact name.

**`get_service_dependencies(service, direction="outgoing", depth=1)`** —
`direction` in `outgoing|incoming|both`; `depth` clamped to `MCP_MAX_DEPTH`.

**`find_api(query=None, path=None, method=None, service=None, limit=None)`**
— at least one filter required.

**`get_api_context(query, service=None)`** — `query` can be an exact
`METHOD /path` or a keyword; best match is used. Includes `ui_callers`
(UI API_CONSTANT nodes that CALLS_API to this endpoint, plus which file
references each one) when the graph has resolved any — requires the
indexed graph to have been built with `SINGLE_DATABASE=true`.

**`resolve_ui_api(query, limit=None)`** — `query` required: a
`ClassName.FIELD` (e.g. `BillingApiConstant.BILLING_CONFIRM_PAYMENT`), a
bare field/class name, or the literal path (`ipd/opd/confirm/payment`).
Returns every matching UI API_CONSTANT with its resolution status —
`resolved`, `unknown_gateway_prefix` (path's first segment isn't a known
backend service), or `no_matching_endpoint` — and the endpoint(s) it
resolves to, if any. Never silently omits an unresolved match.

**`get_business_flow(service, flow)`** — both required; `flow` is a free-text
keyword matched against endpoint paths/handler names first, then method
names if no endpoint matches. When the matched entry point is a REST
endpoint, the result also includes `ui_callers` and `downstream_calls`
(Feign/WebClient/RestTemplate to another backend service, Kafka
publish/consume from the handler method) folded into `chain`.

**`trace_business_flow(target, direction="both", depth=None)`** — `target`
required: a UI API constant, Angular/React component/service, Java
controller/class/method, REST endpoint (`"POST /opd/confirm/payment"`),
Kafka topic, DB table, or Spring Batch Job/Step/Tasklet, matched by name
(exact match wins over fuzzy, regardless of node type — see AGENTS.md
"Business flow tracing"). `direction`: `forward` (downstream effects),
`reverse` (upstream callers), or `both` (default — two separate trees,
never mixed step-by-step into one path). `depth` clamped to
`MCP_MAX_DEPTH`. Every hop carries `confidence` — exactly `VERIFIED`,
`INFERRED`, or `UNRESOLVED` (uppercase) — plus `evidence` (file/line/symbol,
for a VERIFIED fact) or `reason` (why it's INFERRED/UNRESOLVED); a hop is
also tagged `path_type: DIRECT` (depth 1) or `TRANSITIVE` (deeper).
`confidence_summary` gives a hop-count breakdown by confidence across the
whole result — always check it rather than assuming a returned flow is
fully proven. Distinct from `get_business_flow` above (fixed shape: one
service + a free-text flow keyword, controller->service->repository only)
— this is the general-purpose tracer, and the one to reach for whenever
the question is "what does X actually connect to."

**`get_database_impact(service=None, entity=None, api=None, flow=None)`**
— at least one required; resolved in priority order `api > flow > entity > service`.

**`find_callers(target, depth=1)`** / **`find_dependencies(target, direction="outgoing", depth=1)`**
— `target` can be a class/interface/method/component name, an API path/keyword
resolved via the controller, or a service name (switches to the
service-level tree automatically).

**`search_knowledge(query, limit=None)`** — free text.

**`get_source_evidence(node)`** — exact or fuzzy name of any node.

**`get_context_pack(topic, service=None, max_tokens=None)`** — `service`
is optional; when omitted, `search_knowledge` picks the best-matching
service for `topic` first.

## 8. Example queries

```python
find_service(query="appointment")
get_service_context(service="appointment-service")
get_service_dependencies(service="appointment-service", direction="both", depth=2)
find_api(query="cancel appointment")
get_api_context(query="POST /book/appointment")
get_business_flow(service="appointment-service", flow="cancellation")
trace_business_flow(target="BillingApiConstant.BILLING_CONFIRM_PAYMENT")
trace_business_flow(target="price_book", direction="reverse")
trace_business_flow(target="POST_FINANCE_PRICE_BOOK", depth=2)
get_database_impact(api="cancel appointment", service="appointment-service")
find_callers(target="AppointmentService")
find_dependencies(target="AppointmentController")
search_knowledge(query="what happens when an appointment is cancelled")
get_source_evidence(node="AppointmentController")
get_context_pack(topic="appointment cancellation", service="appointment-service", max_tokens=1200)
```

Real output (against a combined dev graph of appointment-service +
auth-service + core-service + ipd-service):

```json
// get_service_context(service="appointment-service")
{
  "service": "appointment-service",
  "purpose": "Java service - 398 classes, 526 methods, 27 REST endpoints",
  "technology": ["Kafka", "Spring Boot", "Spring Cloud OpenFeign", "Spring Data MongoDB"],
  "apis_count": 27,
  "entities_count": 17,
  "tables_count": 20,
  "dependents": ["auth-service", "core-service", "ipd-service"],
  "events": ["BlockSchedularEvent", "IBlockSchedularEvent", "MessageEvent", "SchedulerEvent"]
}
```

```markdown
<!-- get_context_pack(topic="book appointment", service="appointment-service", max_tokens=600) -->
# Book Appointment

## Services
- appointment-service

## API
POST /book/appointment

## Main Classes
- AppointmentController
- bookAppointmentConfig()

## Flow
AppointmentController
→ bookAppointmentConfig
→ AppointmentService
→ FollowupRequestService
→ MessageEventService
→ TokenService
→ BlockSchedularEvent

## Important Relationships
- AppointmentController → AppointmentService
- AppointmentService → FollowupRequestService
- FollowupRequestService → MessageEventService
- MessageEventService → TokenService

## Evidence
- AppointmentController.bookAppointmentConfig()
```

## 9. Token optimization strategy

Every tool follows the same rules, enforced structurally rather than by
convention alone:

- **No full-graph/full-file dumps.** No tool accepts "give me
  everything"; `get_source_evidence` returns a file/line/signature
  pointer, never file contents (see §11 — there's no source-repo access
  at all, only the graph).
- **Depth and result counts are always bounded server-side**
  (`mcp_server/limits.py`: `clamp_depth`, `clamp_limit`) — a caller can
  ask for `depth=999` and get back `MCP_MAX_DEPTH`, not an error, not an
  unbounded traversal.
- **Cycle protection is structural, not incidental.** `queries/common.py`'s
  `traverse()` tracks visited node ids; `class_dependency_chain()`
  tracks visited class ids separately (a circular `@Service` dependency
  — A depends on B depends on A — terminates in 2 hops, not a stack
  overflow; covered by `tests/test_dependency_tools.py::test_circular_dependency_terminates`).
- **Service-level tools never touch a per-class subgraph.**
  `get_service_dependencies` on a service with 20,000 nodes runs a BFS
  over *services* (a handful of rows), not over every class that service
  owns — see `service_queries._adjacent_services`.
- **No unnecessary empty fields** (`models/evidence.py: prune_empty`) —
  applied to every "detail" tool (`get_service_context`,
  `get_api_context`, `get_business_flow`, `get_database_impact`) so an
  empty `dependents: []` is omitted rather than shipped. **Not** applied
  to list/search tools (`find_service`, `find_api`, `search_knowledge`,
  `find_callers`, `find_dependencies`) — there, an empty `"results": []`
  is itself the answer ("searched, found nothing") and must stay.
- **`get_context_pack` has an explicit token budget**
  (`models/context.py: estimate_tokens`, a `len(text)//4` heuristic — no
  tokenizer dependency, same philosophy as the indexer's zero-dependency
  design) and trims lower-priority sections (events, then tables, then
  the services list) before ever truncating the flow itself.
- **No duplicates.** `search_knowledge` dedupes by `(type, name)`;
  `class_dependency_chain` dedupes repository classes by id even when
  reached via multiple paths.

## 10. Security model — read-only

- **SQLite-enforced, not just convention.** `mcp_server/db.py` opens the
  database with `file:...?mode=ro` in the connection URI. A write
  attempt from anywhere in the query layer raises
  `sqlite3.OperationalError: attempt to write a readonly database` —
  verified directly (see "Lessons learned" §1 in this doc's development,
  and `tests/`).
- **No arbitrary SQL or Cypher tool exists.** Every query is a
  parameterized statement inside `queries/*.py`; there is no tool that
  accepts a raw query string (unlike `example_queries.py`'s `sql`
  subcommand, which is a *human* CLI utility, not exposed over MCP).
- **No source-repository access.** The server only ever opens the
  `.db` file at `KG_DB_PATH`; it never reads `ROOT_PATH` or any file the
  indexer scanned. `get_source_evidence` returns a pointer (file path +
  line range + the indexer's captured signature snippet), not file
  contents fetched fresh from disk.
- **No shell/process execution** anywhere in `mcp_server/`.
- **Docker**: `docker-compose.yml`'s `knowledge-graph-mcp` service mounts
  `./output:/app/output:ro` — read-only at the container/volume level
  too, so even a bug in the container image can't touch the file docker
  passes it.

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `db_not_found` error from every tool | `KG_DB_PATH` wrong, or `python indexer.py` never run | Check the path is correct and the file exists; run the indexer |
| Claude Code shows "Failed to connect" / `CONNECTION_CLOSED` | Server invoked as `python mcp_server/server.py` (script mode breaks its relative imports) | Use `python mcp_server_main.py` — see §4 |
| VS Code MCP output shows `ModuleNotFoundError: No module named 'mcp_server'` | Invoked as `python -m mcp_server.server` from a cwd that isn't the repo root (VS Code's MCP client spawns from the workspace root, not this subfolder) | Use `python mcp_server_main.py` with an absolute path — it's cwd-independent, see §4 |
| `pip install -r requirements-mcp.txt` breaks an unrelated tool | Installed into the global/shared Python instead of a venv | Always use `.venv-mcp` (§2); if already polluted, `pip uninstall mcp mcp-types` and re-check `pip check` |
| A tool always returns `{"results": []}` | Query didn't match anything — this is a real "not found", not a bug | Try `find_service`/`find_api` with a broader keyword first |
| `get_database_impact`/`get_business_flow` returns `not_found` for a real API | No `@Service`-annotated class reachable within 3 hops of the controller, or the controller has no resolved `DEPENDS_ON` edges | Expected/honest behavior, not a bug — confirm with `get_api_context` on the same API; it'll show empty `repositories`/`downstream_services` too |
| `query_timeout` error | A query genuinely ran past `MCP_QUERY_TIMEOUT_SECONDS` (default 5s) — unusual for this graph's size | Raise `MCP_QUERY_TIMEOUT_SECONDS`, or check `KG_DB_PATH` isn't pointing at an unexpectedly huge/corrupt file |
| Unicode arrows (`→`) look garbled when you `print()` a result in a Windows terminal | That terminal's codepage (cp1252), not the data | The actual JSON-RPC bytes are UTF-8 (the SDK's `mcp/server/stdio.py` explicitly re-wraps stdio as UTF-8 for exactly this reason) — a real MCP client is unaffected |

## 12. Development guide

**Testing needs no MCP SDK at all.** `tools/*.py` and everything under
`queries/`/`models/` never import `mcp` — only `server.py` and each
`tools/*.py`'s bottom-of-file `register(server)` function do. So
`tests/` calls the plain tool functions directly
(`from mcp_server.tools.service_tools import find_service`) and runs
with a stock Python + `pytest`, no venv/SDK install required:

```powershell
python -m pytest tests/ -v
```

`tests/conftest.py` builds a small synthetic graph using the *real*
`kg_indexer.db.GraphDB` API (not hand-written SQL) — a controller ->
service -> repository chain, a cross-service dependency, a naming-
convention event class, an Angular component, and a circular
`@Service` dependency for cycle-protection testing — so the fixture can
never silently drift from the schema the indexer actually produces, and
no customer database is required.

**Adding a tool**: write the query logic in `queries/`, add a thin
validating wrapper in the matching `tools/*.py` (`@tool_boundary("name")`
decorator, then `@server.tool()` via `register()`), write tests calling
the plain function, and add a row to §6/§7 above.

**Confidence taxonomy** (see `queries/common.py` module docstring and
`models/evidence.py`) — every tool that infers rather than reads a fact
directly tags it:

- `verified` — a real graph edge whose target resolved to a known node,
  or a fact read directly from a parsed annotation/decorator.
- `inferred` — derived here via a documented naming/structural
  heuristic (e.g. guessing a table name by stripping "Repository" off a
  class name), not something the indexer itself asserted.
- `unknown` — a real edge exists (the source really does reference this
  name) but it never resolved to a specific node — usually an external
  library type.
- `not_found` — returned as an error, not a guess, when nothing in the
  graph matches at all.

**Honesty model — what this server does *not* pretend to know:**
- No `CALLS` edges exist, so `find_callers` on a method answers at the
  *containing class* level (who depends on/extends this class), not
  real call-site tracking.
- No `@Table`/`@Column` mapping is captured, so every "table" name in
  `get_database_impact`/`get_api_context` is inferred by stripping
  `Repository`/`Repo` off a class name — always `confidence: inferred`.
- Request/response DTOs in `get_api_context` are a best-effort guess
  (first non-primitive parameter type / the method's stripped return
  type) because the indexer discards which Spring annotation
  (`@RequestBody` vs `@PathVariable`) applied to which parameter during
  parsing — see `queries/api_queries.py` module docstring.
- `class_dependency_chain` walks at most 3 hops
  (`common.MAX_CHAIN_DEPTH`) from a controller looking for a
  Repository — a real chain deeper than that returns empty rather than
  an expensive unbounded search.

### Lessons learned during this build (kept here so they aren't repeated)

1. **A circular `@Service` dependency (A → B → A) previously had no
   cycle protection** in the class-to-repository walk — only the
   generic node `traverse()` tracked visited ids; `class_dependency_chain`
   didn't track visited *class* ids for its own recursion. Fixed by
   adding a `seen_class_ids` set. Covered by
   `test_circular_dependency_terminates`.
2. **Java `@Autowired` parameter parsing had a pre-existing indexer bug**:
   annotation prefixes (`@RequestBody`, `@PathVariable`) leaked into the
   stored parameter *type* string (e.g. `"@RequestBody List"` instead of
   `"List"`), corrupting `get_api_context`'s DTO detection for nearly
   every real Spring endpoint. Fixed in
   `kg_indexer/parsers/java_parser.py` (`PARAM_ANNOTATION_PREFIX_RE`) —
   the one indexer change this feature required, per "don't modify the
   indexer unless absolutely required."
3. **Cross-language edge resolution (in the indexer, not this server)**
   was already fixed in an earlier session for the same reason this
   server needs to be careful: `SINGLE_DATABASE=true` puts every
   language's nodes/edges in one table, so anything that matches by
   simple name (not exact qualified path) must be scoped by
   `project_type`/service or it'll cross-contaminate. This server
   inherits that constraint rather than re-introducing it: it never
   does its own simple-name resolution against the *raw* `edges` table
   without going through the indexer's already-resolved `target_id`.
4. **`pip install mcp[cli]` into the global/shared Python environment
   broke an unrelated fastapi installation** (starlette/h11 version
   conflicts) and, more subtly, broke `pytest` process-wide by
   upgrading `anyio` to a version whose pytest plugin requires a newer
   pytest than this machine had. Always use `.venv-mcp` (§2); if it
   happens anyway, `pip check` shows exactly what's conflicted.
5. **`python mcp_server/server.py` does not work** — relative imports
   inside a package require either `python -m mcp_server.server` (run
   from the repo root) or a proper bootstrap script. Caught by actually
   running `claude mcp add` and `claude mcp get` against this exact repo
   rather than assuming the spec's example invocation was correct — see §5.
6. **`python -m mcp_server.server` isn't actually portable across MCP
   clients** — it only works if the caller's cwd happens to be the repo
   root. Claude Code's CLI does default there; VS Code's built-in MCP
   client (reading the same shared `.mcp.json`) spawns from the
   workspace root one level up instead, and the identical command
   failed with `ModuleNotFoundError: No module named 'mcp_server'`.
   Fixed by adding `mcp_server_main.py` — a tiny root-level script that
   inserts its own directory into `sys.path` before importing, making
   the invocation cwd-independent — and switching every client config
   (Claude Code local scope, the shared `.mcp.json`) to call that
   instead, with absolute paths. Lesson: a fix verified against one MCP
   client doesn't mean it's verified against all of them; the config
   surface (cwd, path resolution) varies per client even when the
   protocol doesn't.
