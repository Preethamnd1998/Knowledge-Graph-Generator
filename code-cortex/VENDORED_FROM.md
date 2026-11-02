# Vendored: code-cortex-mcp

This directory is a vendored copy of an external, MIT-licensed open-source
project — **not** code authored by this repository. It was consolidated
here (previously a separate sibling clone) so Enterprise AI Platform only
needs to clone one repository to get both Knowledge Generator and Code
Cortex.

## Provenance

| | |
|---|---|
| **Upstream repository** | https://github.com/tigercosmos/code-cortex-mcp |
| **Vendored at commit** | `5a653df9b112fe061372df407c69bcc3539964a8` |
| **Vendored at tag** | `v0.21.0` ("docs(release): bump to v0.21.0") |
| **Vendored on** | 2026-09-14 |
| **License** | MIT (see `LICENSE` in this directory) |

`code-cortex-mcp` is itself a C++23 fork of a further upstream C11 project
— see `UPSTREAM_SYNC.md` in this directory (that file is upstream's own,
unmodified) for how *that* sync relationship works. This vendoring is a
separate, one-directional snapshot: we are not set up to push changes back
upstream, and syncing forward from a newer upstream release is a manual,
deliberate action (see "Updating" below), not automatic.

## Local modifications on top of v0.21.0

The full diff is preserved in `LOCAL_MODIFICATIONS.patch` in this
directory. Summary:

- **`src/pipeline/pass_orm_nodes.cpp`** (new file) — JPA (`@Entity`/`@Table`
  → `Table`, `MAPS_TO_TABLE`) and Spring Data MongoDB (`@Document`/`@Field`
  → `MongoCollection`/`MongoField`, `MongoRepository<E,ID>` → `E`,
  `REPOSITORY_FOR`) persistence-mapping passes.
- **`src/pipeline/pipeline.cpp`** — registers the new ORM/Mongo passes in
  the full-index pipeline; fixed a pre-existing bug where `PREDUMP_PASS_COUNT`
  was a hardcoded `enum` that silently dropped array entries past index 6
  (now `sizeof(passes)/sizeof(passes[0])`).
- **`src/pipeline/pipeline_incremental.cpp`** — added the same ORM/Mongo/
  route/complexity passes to the incremental path's `run_postpasses`,
  which had drifted out of sync with the full-index pass list (a real,
  narrow pre-existing bug, not a new capability).
- **`src/pipeline/pipeline_internal.h`** — declarations for the above.
- **`CMakeLists.txt`** — registers the new source file in the build.

Everything else in this directory (`internal/`, `src/` besides the four
files above, `tests/`, `docs/`, `vendored/`, etc.) is unmodified upstream
code at the pinned commit.

## Updating to a newer upstream release

This is a manual, deliberate action — never automatic, never part of
`install/bootstrap.sh`:

```bash
# From a scratch clone of the upstream repo (not this vendored copy):
git clone https://github.com/tigercosmos/code-cortex-mcp.git /tmp/cortex-upstream
cd /tmp/cortex-upstream
git checkout <new-tag-or-commit>
git apply /path/to/knowledge-graph-generator/code-cortex/LOCAL_MODIFICATIONS.patch
# Resolve any conflicts, re-test (see ../tests/ in this repo for the
# integration tests that exercise the persistence-mapping passes), then
# replace this directory's contents and update the provenance table above.
```

## Why vendor a working tree instead of merging git histories

The upstream project has 893 commits from 30+ external contributors and
its own release/CI history — importing that wholesale into a private
Lifetrenz repository would mix unrelated project history and complicate
license/attribution tracking for no real benefit, since we don't intend
to contribute changes back upstream through this repo. A pinned-commit
vendored snapshot + an explicit patch file is the standard approach for
this situation and keeps this repository's own git history meaningful
(commits here are about Knowledge Generator + Code Cortex integration
work, not upstream's).
