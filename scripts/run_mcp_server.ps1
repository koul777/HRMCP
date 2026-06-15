param(
    [string]$DbPath = "",
    [switch]$Check
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Src = Join-Path $Root "src"

if (-not $DbPath) {
    $DbPath = Join-Path $Root "data\processed\ncs.db"
}

if (-not (Test-Path $DbPath)) {
    throw "NCS SQLite DB not found: $DbPath"
}

$env:PYTHONPATH = $Src
$env:NCS_DB_PATH = (Resolve-Path $DbPath).Path
$env:PYTHONUTF8 = "1"

Set-Location $Root

if ($Check) {
    python -c "from ncs_mcp.config import load_settings; s=load_settings(); print(s.db_path)"
    exit $LASTEXITCODE
}

python -m ncs_mcp.server
