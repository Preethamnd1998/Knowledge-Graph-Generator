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

## Quick start

1. Open `kg-config.env` in any text editor.
2. Comment out (`#`) any service or Angular sub-project you want to
   **skip**. Remove the `#` to include one.
3. Save, then run:

   ```
   python indexer.py
   ```

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
from `@*Mapping` (HTTP verb + full path), `extends`/`implements`, and
`@Autowired`/`@Inject`/`@Resource` field dependencies.

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
```

Or import it directly for agent/programmatic use:

```python
from example_queries import connect, get_dependencies
conn = connect("output/knowledge_graph.db")
print(get_dependencies(conn, "PatientController"))
```

## Running in Docker

```
docker compose run --rm kg-indexer
```

Edit the volume mount in `docker-compose.yml` if your checkout isn't at
`D:/GitLab-Workspace/UI-BE`. The `.db` file(s) appear in `./output` on
the host.

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
