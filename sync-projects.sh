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
# Discovery (no scan of source): BE = Projects/BE/*/pom.xml, UI = Projects/UI/*/angular.json.
# Safe to re-run: both indexers skip unchanged files, so an up-to-date workspace
# takes seconds-to-minutes, not a full rebuild. kg-config.env is never modified —
# the Knowledge Graph runs from throwaway configs via KG_CONFIG_PATH, which is what
# keeps each UI repo's data separate (see the UI-project note in kg-config.env).
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

BE=(); for d in "$ROOT_PATH"/BE/*/; do [[ -f "${d}pom.xml" ]] && BE+=("BE/$(basename "$d")"); done
UI=(); for d in "$ROOT_PATH"/UI/*/; do [[ -f "${d}angular.json" ]] && UI+=("UI/$(basename "$d")"); done
log "found ${#BE[@]} backend service(s), ${#UI[@]} UI project(s) under $ROOT_PATH"
printf '  %s\n' "${BE[@]}" "${UI[@]}"
[[ $DRY -eq 1 ]] && exit 0

# ---- Knowledge Graph ---------------------------------------------------------
write_config() {   # $1 = file, $2 = index mode, $3 = angular project ("" for none)
  {
    echo "ROOT_PATH=$ROOT_PATH"
    echo "BACKEND_SERVICES_START"; printf '%s\n' "${BE[@]}"; echo "BACKEND_SERVICES_END"
    echo "ANGULAR_PROJECT=$3"
    echo "ANGULAR_SUBPROJECTS_START"
    if [[ -n "$3" ]]; then
      echo "ROOT_APP"
      for p in "$ROOT_PATH/$3"/projects/*/; do [[ -d "$p" ]] && basename "$p"; done
    fi
    echo "ANGULAR_SUBPROJECTS_END"
    echo "REACT_PROJECT_PATH="
    echo "INDEX_MODE=$2"
    echo "OUTPUT_DIR=$OUTPUT_DIR"
    echo "SINGLE_DATABASE=true"
  } > "$1"
}

if [[ $DO_KG -eq 1 ]]; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  first=1
  for ui in "${UI[@]}"; do
    # First run also (re)indexes the backend so gateway prefixes exist for
    # constant->endpoint resolution; later UI runs are frontend-only.
    write_config "$TMP/kg.env" "$([[ $first -eq 1 ]] && echo selective || echo frontend_only)" "$ui"
    first=0
    log "Knowledge Graph: $ui"
    (cd "$HERE" && KG_CONFIG_PATH="$TMP/kg.env" "$PY" indexer.py > "$TMP/$(basename "$ui").log" 2>&1) \
      && log "  ok — $(grep -E 'Targets indexed' "$TMP/$(basename "$ui").log" | tr -s ' ')" \
      || { log "  FAILED — see $TMP/$(basename "$ui").log"; tail -5 "$TMP/$(basename "$ui").log"; }
  done
  if [[ ${#UI[@]} -eq 0 && ${#BE[@]} -gt 0 ]]; then
    write_config "$TMP/kg.env" backend_only ""
    (cd "$HERE" && KG_CONFIG_PATH="$TMP/kg.env" "$PY" indexer.py > "$TMP/be.log" 2>&1) && log "Knowledge Graph: backend ok" || log "Knowledge Graph: backend FAILED ($TMP/be.log)"
  fi
fi

# ---- Code Cortex (one project per repo; unified-trace auto-discovers them) ----
if [[ $DO_CORTEX -eq 1 ]]; then
  CORTEX_BIN="${CODE_CORTEX_PATH:-$(command -v code-cortex-mcp 2>/dev/null || true)}"
  [[ -z "$CORTEX_BIN" && -x "$HOME/.local/bin/code-cortex-mcp" ]] && CORTEX_BIN="$HOME/.local/bin/code-cortex-mcp"
  if [[ -z "$CORTEX_BIN" ]]; then
    log "code-cortex-mcp not installed — skipping (run: npm run platform:install -- --apply --with-cortex)"
  else
    for p in "${BE[@]}" "${UI[@]}"; do
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
