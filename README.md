# Knowledge Graph Indexer

Builds a SQLite code knowledge graph across the Java microservices,
Angular sub-projects, and (optionally) a React app that make up this
workspace — so an AI agent or a human can query "what depends on what"
without re-reading the whole codebase every time.

> **Building or extending this yourself?** See [`AGENTS.md`](./AGENTS.md)
> for the graph schema reference, query cookbook, and where the parsing
> logic lives — written for an AI agent (or a developer) that needs to
> work with this tool without re-deriving its conventions from scratch.

## Workspace layout this tool expects

This indexer was built for, and is pre-configured for, the following
layout. If your workspace differs, only `kg-config.env` needs to change
— nothing else.

```
D:\GitLab-Workspace\UI-BE                        <- ROOT_PATH (backend root)
├── appointment-service, auth-service, core-service, ...   <- each a BACKEND_SERVICES entry (Java/Maven)
├── lifetrenz-core                                <- ANGULAR_PROJECT (Angular monorepo root)
│   ├── src\app\...                               <- the monorepo's own "shell" app — index via ROOT_APP
│   └── projects\
│       ├── ambulance, settings, masters, ...      <- each an ANGULAR_SUBPROJECTS entry
│       └── ...
└── (no React app in this workspace yet — REACT_PROJECT_PATH left blank)
```

- **Backend root**: `D:\GitLab-Workspace\UI-BE` — every immediate
  sub-folder with its own `pom.xml`/`src` is a candidate for
  `BACKEND_SERVICES`.
- **UI (Angular) root**: `D:\GitLab-Workspace\UI-BE\lifetrenz-core` —
  the monorepo itself has a root app (`src/`, indexed via the special
  `ROOT_APP` entry) plus many feature sub-projects under
  `lifetrenz-core\projects\*`.
- **React**: not present in this workspace as of writing; set
  `REACT_PROJECT_PATH` if/when one exists (any absolute path, doesn't
  have to live under `ROOT_PATH`).

This repo (`D:\GitLab-Workspace\Knowledge Graph Generator`) is
intentionally separate from the code it indexes — it never writes into
`ROOT_PATH`, only reads from it.

## Prerequisites

Python 3.8+ is the only requirement — the indexer itself has zero
third-party dependencies. Check whether you already have it, from a
Command Prompt (`cmd`):

```
python --version
```

If that fails with `'python' is not recognized...`, install it (Windows
10/11, from `cmd`, no admin rights needed):

```
winget install --id Python.Python.3.12 -e --source winget
```

`winget` ships with modern Windows by default. Close and reopen `cmd`
afterward so it picks up the new `PATH`, then re-check with
`python --version`.

If `winget` itself isn't recognized (older Windows, or it's disabled by
policy), install from the official source instead: download the
Windows installer from <https://www.python.org/downloads/>, run it, and
**tick "Add python.exe to PATH"** on the first screen of the installer —
that checkbox is what makes `python` work from `cmd` afterward.

The MCP server (`mcp_server/`, see below) has its own, separate
installation step — it needs the `mcp` SDK, installed into its own
virtual environment rather than system-wide. See [`MCP.md`](./MCP.md) §2.

## Quick start

1. Open `kg-config.env` in any text editor.
2. Comment out (`#`) any service or Angular sub-project you want to
   **skip**. Remove the `#` to include one.
3. Save, then run it — either:

   ```
   python indexer.py
   ```

   or double-click **`run-knowledge-graph.bat`** (no command line
   needed). It works as-is sitting in this folder. If you move the
   `.bat` file somewhere else, open it in a text editor (right-click ->
   Edit) and set the `KG_ROOT` line near the top to the full path of
   this folder — everything else about it stays the same.

No command-line flags — everything comes from `kg-config.env`. The graph
is written to the `output/` folder (or wherever `OUTPUT_DIR` points).
Re-running is safe: each target's `.db` file is fully rebuilt from
scratch every run (see "Re-running / incremental behavior" below), never
duplicated or appended to.

## Configuring `kg-config.env`

| Key | Meaning |
|---|---|
| `ROOT_PATH` | Folder containing all the backend microservices |
| `BACKEND_SERVICES_START` / `_END` block | One Java service folder name per line; `#` a line to skip it |
| `ANGULAR_PROJECT` | Name of the Angular monorepo folder (contains `projects/`) |
| `ANGULAR_SUBPROJECTS_START` / `_END` block | One sub-project folder name per line; `#` a line to skip it. The special entry `ROOT_APP` (not a real folder) indexes `ANGULAR_PROJECT`'s own root `src/` — the main app shell that lives outside `projects/` and would otherwise never get indexed |
| `REACT_PROJECT_PATH` | Absolute path to a React app, or blank to skip React entirely |
| `INDEX_MODE` | `all`, `backend_only`, `frontend_only`, or `selective` |
| `OUTPUT_DIR` | Where the `.db` file(s) are written |
| `SINGLE_DATABASE` | `true` = one combined `knowledge_graph.db`; `false` = one `.db` per service/sub-project |

`ROOT_PATH`, `OUTPUT_DIR`, and `REACT_PROJECT_PATH` can also be set via
environment variables of the same name — an env var wins over the file.
This is how `docker-compose.yml` points the indexer at a mounted volume
without editing the config file.

### Adding a new service or sub-project later

When a new microservice or Angular feature module is added to the
workspace, add one line for it inside the matching `_START`/`_END` block
in `kg-config.env` (uncommented, to include it immediately — or
commented, to leave it available but off). No code changes needed.

### Re-running / incremental behavior

Every run does a **full reindex**: for each enabled target, the matching
`.db` file is deleted and rebuilt from the current state of the code —
never appended to or duplicated. If you disable a target that was
previously indexed, its old `.db` file is simply left behind in
`output/` (it isn't cleaned up automatically); delete `output/` yourself
first if you want it to reflect only the current config.

## What gets extracted

**Java** — classes/interfaces/enums, class annotations
(`@RestController`, `@Service`, `@Entity`, ...), methods, REST endpoints
from `@*Mapping` (HTTP verb + full path, including a path given as a
symbolic `SomeApiConstant.FIELD` reference rather than a string literal),
`extends`/`implements`, `@Autowired`/`@Inject`/`@Resource` field
dependencies, and `@FeignClient` interface methods (as outgoing calls to
another service, not endpoints of their own).

**UI ↔ Backend API mapping** — an Angular/React `FooApiConstant.FIELD =
"path"` constant is matched to the real Java controller endpoint it calls,
via each backend service's own registered API Gateway prefix
(`spring.application.name`) plus its controller's `@RequestMapping` +
method `@*Mapping` path; a `@FeignClient`/WebClient/RestTemplate call in
one backend service is matched to another service's endpoint the same
way. Every match states its confidence and, when it can't resolve, *why*
(unknown gateway prefix, no matching endpoint, ambiguous multiple
matches, ...) rather than guessing — see AGENTS.md's "UI -> Backend API
mapping" section for the full mechanism. Requires `SINGLE_DATABASE=true`.

**Java runtime flows** (`kg_indexer/parsers/spring_flow_parser.py`, an
additive second pass) — Spring Batch `@Bean Job`/`Step`/`Tasklet`
definitions and how they chain together (`JobBuilder.start()`/`.next()`,
`StepBuilder.tasklet()`), `jobLauncher.run(...)` call sites resolved
back to the specific job bean via `@Qualifier`, `@Async` methods and
`CompletableFuture.runAsync`/`supplyAsync` call sites, Kafka producers
(`producerService.publishToKafka(...)`) and `@KafkaListener` consumers
resolved to a shared, service-independent topic identity, and
best-effort database table reads/writes recognized from literal
`INSERT INTO`/`UPDATE ... SET`/`DELETE FROM`/`SELECT ... FROM` SQL
(lowercase snake_case table names only — a JPQL/HQL entity reference
like `select o from Order o` is deliberately excluded, not mistaken for
a real table). See that module's docstring for the full node/edge
vocabulary and the `verified`/`inferred`/`unknown` confidence model
applied to every edge it creates.

**Angular** — `@Component`, `@NgModule` (with `declarations`/`imports`/
`exports`), `@Injectable`, `@Directive`, `@Pipe`, constructor-injected
dependencies, and local (relative-path) ES module imports.

**React** — function and class components, custom hooks (`useXxx`
definitions), which built-in/custom hooks each component calls, which
other components it renders (JSX composition), and what's exported.

Everything is stored as a single **nodes** table and **edges** table
(see `kg_indexer/db.py`, and the schema reference in `AGENTS.md`) rather
than one table per language, so a query like "what does X depend on"
works the same way across Java, Angular, and React. Edges whose target
lives in a file that hadn't been parsed yet are resolved in a second
pass at the end of the run; edges that still can't be resolved (external
libraries, dynamic wiring) are kept with just a `target_name` so they
remain visible.

## Querying the graph

```
python example_queries.py summary
python example_queries.py endpoints --service core-service
python example_queries.py search PatientController
python example_queries.py deps PatientController
python example_queries.py dependents PatientRepository
python example_queries.py hooks useEffect
python example_queries.py cross-service
python example_queries.py sql "SELECT * FROM nodes WHERE node_type='ENDPOINT' LIMIT 20"

# Spring Batch / Kafka / async runtime flows
python example_queries.py jobs
python example_queries.py job createPriceBookFromTempJob
python example_queries.py kafka
python example_queries.py topic POST_FINANCE_PRICE_BOOK
python example_queries.py async
python example_queries.py table price_book
python example_queries.py flow createPriceBookFromTempJob

# UI <-> Backend API mapping
python example_queries.py api-constants CONFIRM_PAYMENT
python example_queries.py ui-to-api "ipd/opd/confirm/payment"
python example_queries.py api-callers ipd-service POST /opd/confirm/payment
python example_queries.py service-calls --service core-service
python example_queries.py full-flow ipd-service POST /opd/confirm/payment
```

Or import it directly for agent/programmatic use:

```python
from example_queries import connect, get_dependencies
conn = connect("output/knowledge_graph.db")
print(get_dependencies(conn, "PatientController"))
```

## Knowledge Graph MCP

The generated graph can be handed to an AI agent (Claude Code, Claude
Desktop, or any other MCP-compatible client) through the **Knowledge
Graph MCP server** in `mcp_server/` — without ever exposing the raw
`nodes`/`edges` schema, and without shipping the whole graph to the
model. The agent calls semantic tools instead of writing SQL:

```python
find_service(query="appointment")
get_service_context(service="appointment-service")
get_business_flow(service="appointment-service", flow="cancellation")
get_context_pack(topic="appointment cancellation", max_tokens=1200)
resolve_ui_api(query="ipd/opd/confirm/payment")  # "which backend endpoint does this UI API constant call?"
```

`get_service_context` returns a handful of fields (purpose, tech,
counts, dependencies), not a dump of every class; `get_context_pack`
returns a compressed Markdown package built specifically for an LLM to
read in one call, budgeted to a token limit. The server is strictly
read-only — no tool can write to, or run arbitrary SQL against, the
graph.

```
python -m venv .venv-mcp
.venv-mcp\Scripts\activate
pip install -r requirements-mcp.txt
claude mcp add knowledge-graph -s local -e KG_DB_PATH=./output/knowledge_graph.db -- "<repo>\.venv-mcp\Scripts\python.exe" "<repo>\mcp_server_main.py"
```

Use `mcp_server_main.py` (a small bootstrap script at the repo root),
not `mcp_server/server.py` directly and not `python -m mcp_server.server`
— the latter only works if the MCP client happens to spawn the process
with the repo root as its working directory, which isn't true for every
client (see `MCP.md` §4/§11 for the VS Code case this bit us on).

Full architecture, all 12 tools with parameters, the confidence model
(`verified`/`inferred`/`unknown`), Claude Code/Desktop configuration,
security model, and troubleshooting: see **[`MCP.md`](./MCP.md)**.

## Running in Docker

```
docker compose run --rm kg-indexer
```

Edit the volume mount in `docker-compose.yml` if your checkout isn't at
`D:/GitLab-Workspace/UI-BE`. The `.db` file(s) appear in `./output` on
the host.

The MCP server has its own container (`knowledge-graph-mcp` in the same
`docker-compose.yml`, built from `Dockerfile.mcp`), mounting `./output`
**read-only** — see `MCP.md` "Running in Docker" for how an MCP client
talks to a containerized stdio server.

## Project structure (for maintainers)

```
indexer.py                       entry point — no CLI args, reads kg-config.env
example_queries.py                importable query helpers + CLI
kg_indexer/
  config.py                       kg-config.env loader (KEY=VALUE + commentable list blocks)
  db.py                            SQLite schema + GraphDB manager (nodes/edges, resolution passes)
  orchestrator.py                  wires config -> parsers -> db, prints progress/summary
  parsers/
    common.py                      shared regex/text helpers (bracket matching, name registry, ...)
    java_parser.py                 Java: classes, methods, REST endpoints, DI edges
    spring_flow_parser.py          Java runtime flows: Spring Batch, Kafka, async, DB tables (additive 2nd pass)
    angular_parser.py              Angular: components/modules/services, DI edges, ROOT_APP handling
    react_parser.py                React: components, hooks, JSX composition
```

## Known limitations

Parsing is regex/text based (no `javalang`/tree-sitter/TypeScript
compiler dependency), which keeps the tool dependency-free but means it
is best-effort, not a full AST:

- Nested inner classes are attributed to their enclosing top-level class.
- Overloaded methods share a qualified name distinguished only by
  parameter types as written in source (no type resolution).
- Angular `@NgModule` arrays (`declarations`, `imports`, `exports`) are
  extracted by scanning for capitalized identifiers inside the array —
  works for typical usage, not for deeply computed values.
- React components written as single-expression arrow functions with no
  `{ }` block body are not detected (only block-bodied components are).
- Java `import` statements resolve by exact fully-qualified path (so a
  library import never gets confused with an unrelated local class that
  happens to share its simple name). `extends`, `@Autowired`/constructor
  injection, and Angular/React JSX composition still resolve by simple
  name only — if the same short name exists in multiple services, the
  edge is resolved to one candidate and the others are recorded in
  `metadata.ambiguous_candidates`.
- Spring Batch/Kafka/async flows (`spring_flow_parser.py`): `Job`/`Step`
  builder references (`.start()`, `.next()`, `.tasklet()`) resolve
  immediately within the same `@Configuration` class (the common
  convention — a job and its steps defined together); if not found
  there, they fall back to the same project-wide simple-name registry
  every other edge type uses, so a step defined in a different class
  can still resolve, with the same "first match wins,
  `metadata.ambiguous_candidates` if more than one" caveat as elsewhere.
  `jobLauncher.run(...)` resolves its job argument through a
  `@Qualifier("beanName")`-annotated field when present, or the bare
  variable/method name otherwise; unresolvable references keep only
  `target_name` (never guessed). A Kafka topic's identity is the
  constant's simple name (e.g. `POST_FINANCE_PRICE_BOOK`), not its
  resolved string value — two differently-named constants that happen to
  hold the same literal topic string will not be merged. Database table
  extraction only recognizes literal SQL keywords with a lowercase
  snake_case table name; a table name behind a variable, a
  `SELECT`-returning function call in a `FROM` clause (e.g. Postgres
  `jsonb_array_elements(...)`), or a CTE alias can still produce a false
  positive — kept deliberately simple rather than building a SQL parser.
