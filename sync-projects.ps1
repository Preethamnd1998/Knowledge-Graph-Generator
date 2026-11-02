# Windows/PowerShell equivalent of sync-projects.sh: index every BE (pom.xml) and
# UI (angular.json) project under the projects root into the Knowledge Graph and
# Code Cortex. No project names to maintain.
#   .\sync-projects.ps1 [-SkipKg] [-SkipCortex] [-DryRun]
# Projects root: $env:PROJECTS_ROOT > kg-config.env ROOT_PATH > PROJECTS_DIR in
# paths.local.env / paths.env. NOTE: authored without access to a Windows machine;
# logic mirrors the .sh — verify on Windows before relying on it.
param([switch]$SkipKg, [switch]$SkipCortex, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Platform = (Resolve-Path (Join-Path $Here '..\..\..')).Path

function Read-Key($file, $key) {
  if (-not (Test-Path -LiteralPath $file)) { return '' }
  $line = Get-Content -LiteralPath $file | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
  if ($line) { return ($line -replace "^$key=", '').Trim() } else { return '' }
}

$Root = $env:PROJECTS_ROOT
if (-not $Root) { $Root = Read-Key (Join-Path $Here 'kg-config.env') 'ROOT_PATH' }
if (-not $Root) { $Root = Read-Key (Join-Path $Platform 'paths.local.env') 'PROJECTS_DIR' }
if (-not $Root) { $Root = Read-Key (Join-Path $Platform 'paths.env') 'PROJECTS_DIR' }
if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) {
  Write-Error '[sync] projects root not set or missing. Set PROJECTS_DIR in paths.local.env (or set $env:PROJECTS_ROOT).'
}

$Out = if ($env:KG_OUTPUT_DIR) { $env:KG_OUTPUT_DIR } else { Join-Path $Here 'output' }
$Py = Join-Path $Here '.venv-mcp\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Py)) { $Py = 'python' }

$BE = @(Get-ChildItem -LiteralPath (Join-Path $Root 'BE') -Directory -ErrorAction SilentlyContinue |
  Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'pom.xml') } | ForEach-Object { "BE/$($_.Name)" })
$UI = @(Get-ChildItem -LiteralPath (Join-Path $Root 'UI') -Directory -ErrorAction SilentlyContinue |
  Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'angular.json') } | ForEach-Object { "UI/$($_.Name)" })
Write-Host "[sync] found $($BE.Count) backend service(s), $($UI.Count) UI project(s) under $Root"
($BE + $UI) | ForEach-Object { Write-Host "  $_" }
if ($DryRun) { exit 0 }

function Write-KgConfig($file, $mode, $ui) {
  $lines = @("ROOT_PATH=$Root", 'BACKEND_SERVICES_START') + $BE + @('BACKEND_SERVICES_END', "ANGULAR_PROJECT=$ui", 'ANGULAR_SUBPROJECTS_START')
  if ($ui) {
    $lines += 'ROOT_APP'
    $lines += @(Get-ChildItem -LiteralPath (Join-Path $Root "$ui\projects") -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
  }
  $lines += @('ANGULAR_SUBPROJECTS_END', 'REACT_PROJECT_PATH=', "INDEX_MODE=$mode", "OUTPUT_DIR=$Out", 'SINGLE_DATABASE=true')
  Set-Content -LiteralPath $file -Value $lines -Encoding ASCII
}

if (-not $SkipKg) {
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("kg-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  try {
    $first = $true
    foreach ($ui in $UI) {
      Write-KgConfig (Join-Path $tmp 'kg.env') ($(if ($first) { 'selective' } else { 'frontend_only' })) $ui
      $first = $false
      Write-Host "[sync] Knowledge Graph: $ui"
      Push-Location $Here
      $env:KG_CONFIG_PATH = Join-Path $tmp 'kg.env'
      & $Py indexer.py *> (Join-Path $tmp 'idx.log')
      $rc = $LASTEXITCODE
      Pop-Location
      if ($rc -eq 0) { Write-Host '  ok' } else { Write-Host "  FAILED (exit $rc) — $tmp\idx.log"; Get-Content (Join-Path $tmp 'idx.log') -Tail 5 }
    }
    if ($UI.Count -eq 0 -and $BE.Count -gt 0) {
      Write-KgConfig (Join-Path $tmp 'kg.env') 'backend_only' ''
      Push-Location $Here; $env:KG_CONFIG_PATH = Join-Path $tmp 'kg.env'; & $Py indexer.py; Pop-Location
    }
  } finally { Remove-Item Env:KG_CONFIG_PATH -ErrorAction SilentlyContinue; Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}

if (-not $SkipCortex) {
  $bin = $env:CODE_CORTEX_PATH
  if (-not $bin) {
    $cmd = Get-Command code-cortex-mcp -ErrorAction SilentlyContinue
    if ($cmd) { $bin = $cmd.Source }
  }
  if (-not $bin -and $env:LOCALAPPDATA) {
    $cand = Join-Path $env:LOCALAPPDATA 'Programs\code-cortex-mcp\code-cortex-mcp.exe'
    if (Test-Path -LiteralPath $cand) { $bin = $cand }
  }
  if (-not $bin) { Write-Host '[sync] code-cortex-mcp not installed — skipping (npm run platform:install -- --apply --with-cortex)' }
  else {
    foreach ($p in ($BE + $UI)) {
      Write-Host "[sync] Code Cortex: $p"
      $repo = (Join-Path $Root $p) -replace '\\', '/'
      $json = '{"repo_path":"' + $repo + '","mode":"fast"}'
      $res = & $bin cli index_repository $json 2>$null
      try { $d = $res | ConvertFrom-Json; Write-Host "  status=$($d.status) nodes=$($d.nodes) edges=$($d.edges)" } catch { Write-Host '  no/invalid response' }
    }
  }
}
Write-Host '[sync] done. Check with the unified-trace index_status tool.'
