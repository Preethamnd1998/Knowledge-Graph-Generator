#!/usr/bin/env bash
# Index EVERY UI and BE project found on disk into both the Knowledge Graph and
# Code Cortex — no project/repo names to type or maintain. A newly cloned or
# newly split-out repo is picked up just by running this again.
#
#   bash sync-projects.sh                 # everything (both indexers)
#   bash sync-projects.sh --skip-cortex   # Knowledge Graph only
#   bash sync-projects.sh --skip-kg       # Code Cortex only
#   bash sync-projects.sh --dry-run       # show what would be indexed
#
# Discovery (no scan of source), under ROOT_PATH: BE roots (default BE) = */pom.xml;
# UI roots (default UI and WAPP) = */angular.json (monolith, split repo with shell +
# projects/*, or standalone app) or */package.json depending on react. Override the roots
# with KG_BE_ROOTS / KG_UI_ROOTS. Python backends (requirements.txt) go to Code Cortex only.
# Safe to re-run: both indexers skip unchanged files, so an up-to-date workspace
# takes seconds-to-minutes, not a full rebuild. kg-config.env is never modified —
# the Knowledge Graph runs from a throwaway config via KG_CONFIG_PATH.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PLATFORM_ROOT="$(cd "$HERE/../../.." 2>/dev/null && pwd)"
# Projects root: $PROJECTS_ROOT > kg-config.env ROOT_PATH > PROJECTS_DIR in
# paths.local.env / paths.env (platform repo root). No default personal path.
read_key() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -1 | tr -d '\r'; }
ROOT_PATH="${PROJECTS_ROOT:-$(read_key "$HERE/kg-config.env" ROOT_PATH)}"
[[ -z "$ROOT_PATH" ]] && ROOT_PATH="$(read_key "$PLATFORM_ROOT/paths.local.env" PROJECTS_DIR)"
[[ -z "$ROOT_PATH" ]] && ROOT_PATH="$(read_key "$PLATFORM_ROOT/paths.env" PROJECTS_DIR)"
if [[ -z "$ROOT_PATH" || ! -d "$ROOT_PATH" ]]; then
  echo "[sync] projects root not set or missing. Set PROJECTS_DIR in paths.local.env (or export PROJECTS_ROOT)." >&2
  exit 2
fi
OUTPUT_DIR="${KG_OUTPUT_DIR:-$HERE/output}"   # override to index into an isolated location
DO_KG=1; DO_CORTEX=1; DRY=0
for a in "$@"; do
  case "$a" in
    --skip-kg) DO_KG=0 ;;
    --skip-cortex) DO_CORTEX=0 ;;
    --dry-run) DRY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

log() { echo "[sync] $*"; }
PY="$HERE/.venv-mcp/bin/python"; [[ -x "$PY" ]] || PY="python3"

# Roots to scan, relative to ROOT_PATH. Default "BE" and "UI WAPP" (whichever exist);
# override with KG_BE_ROOTS / KG_UI_ROOTS, e.g.  KG_UI_ROOTS="WAPP" bash sync-projects.sh
BE_ROOTS=(); for r in ${KG_BE_ROOTS:-BE}; do [[ -d "$ROOT_PATH/$r" ]] && BE_ROOTS+=("$r"); done
UI_ROOTS=(); for r in ${KG_UI_ROOTS:-UI WAPP}; do [[ -d "$ROOT_PATH/$r" ]] && UI_ROOTS+=("$r"); done
# BE = Java (pom.xml); PYBE = Python (Code Cortex only: the Knowledge Graph has no Python parser);
# UI = Angular (angular.json) or React (package.json with react) — monolith, split repo or standalone.
BE=(); PYBE=(); UI=()
for r in ${BE_ROOTS[@]+"${BE_ROOTS[@]}"}; do for d in "$ROOT_PATH/$r"/*/; do
  if [[ -f "${d}pom.xml" ]]; then BE+=("$r/$(basename "$d")")
  elif [[ -f "${d}requirements.txt" || -f "${d}pyproject.toml" ]]; then PYBE+=("$r/$(basename "$d")"); fi
done; done
for r in ${UI_ROOTS[@]+"${UI_ROOTS[@]}"}; do for d in "$ROOT_PATH/$r"/*/; do
  if [[ -f "${d}angular.json" ]] || { [[ -f "${d}package.json" ]] && grep -q '"react"' "${d}package.json"; }; then
    UI+=("$r/$(basename "$d")"); fi
done; done
log "found ${#BE[@]} Java service(s), ${#PYBE[@]} Python service(s), ${#UI[@]} UI project(s) under $ROOT_PATH"
printf '  %s\n' ${BE[@]+"${BE[@]}"} ${PYBE[@]+"${PYBE[@]}"} ${UI[@]+"${UI[@]}"}
[[ $DRY -eq 1 ]] && exit 0

# ---- Knowledge Graph ---------------------------------------------------------
# One run: the indexer discovers every project under the roots itself (kg_indexer/discovery.py),
# qualifies each split repo's sub-projects by repo name so they never overwrite each other, and
# resolves UI constants -> backend endpoints across all of them together.
write_config() {   # $1 = file
  {
    echo "ROOT_PATH=$ROOT_PATH"
    echo "BACKEND_ROOT=$(IFS=,; echo "${BE_ROOTS[*]-}")"
    echo "UI_ROOT=$(IFS=,; echo "${UI_ROOTS[*]-}")"
    echo "INDEX_MODE=all"
    echo "OUTPUT_DIR=$OUTPUT_DIR"
    echo "SINGLE_DATABASE=true"
  } > "$1"
}

if [[ $DO_KG -eq 1 ]]; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  write_config "$TMP/kg.env"
  log "Knowledge Graph: ${#BE[@]} backend(s) + ${#UI[@]} UI project(s), one run"
  (cd "$HERE" && KG_CONFIG_PATH="$TMP/kg.env" "$PY" indexer.py > "$TMP/kg.log" 2>&1) \
    && { log "  ok — $(grep -E 'Targets indexed' "$TMP/kg.log" | tr -s ' ')"; grep -E '^\[discovery\]' "$TMP/kg.log" | sed 's/^/  /'; } \
    || { log "  FAILED — see $TMP/kg.log"; tail -5 "$TMP/kg.log"; }
fi

# ---- Code Cortex (one project per repo; unified-trace auto-discovers them) ----
if [[ $DO_CORTEX -eq 1 ]]; then
  CORTEX_BIN="${CODE_CORTEX_PATH:-$(command -v code-cortex-mcp 2>/dev/null || true)}"
  [[ -z "$CORTEX_BIN" && -x "$HOME/.local/bin/code-cortex-mcp" ]] && CORTEX_BIN="$HOME/.local/bin/code-cortex-mcp"
  if [[ -z "$CORTEX_BIN" ]]; then
    log "code-cortex-mcp not installed — skipping (run: npm run platform:install -- --apply --with-cortex)"
  else
    for p in ${BE[@]+"${BE[@]}"} ${PYBE[@]+"${PYBE[@]}"} ${UI[@]+"${UI[@]}"}; do
      log "Code Cortex: $p"
      out="$("$CORTEX_BIN" cli index_repository "{\"repo_path\":\"$ROOT_PATH/$p\",\"mode\":\"fast\"}" 2>/dev/null)"
      echo "$out" | "$PY" -c 'import sys,json
try:
    d=json.load(sys.stdin); print("  status=%s nodes=%s edges=%s" % (d.get("status"), d.get("nodes"), d.get("edges")))
except Exception:
    print("  no/invalid response")'
    done
  fi
fi
log "done. Check with the unified-trace index_status tool (READY / STALE per project)."
