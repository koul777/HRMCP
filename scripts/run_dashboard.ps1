param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$NoOpen,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonPath = Join-Path $Root "src"
$Dashboard = Join-Path $Root "scripts\ncs_dashboard.py"
$Logs = Join-Path $Root "logs"
$OutLog = Join-Path $Logs "dashboard.out.log"
$ErrLog = Join-Path $Logs "dashboard.err.log"
$Url = "http://$HostAddress`:$Port"

New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$env:PYTHONPATH = $PythonPath

$existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -First 1

if ($Restart -and $existing) {
    Stop-Process -Id $existing.OwningProcess -Force
    Start-Sleep -Seconds 1
    $existing = $null
}

if (-not $existing) {
    $python = (Get-Command python -ErrorAction Stop).Source
    $ArgsLine = "-u `"$Dashboard`" --host `"$HostAddress`" --port $Port"
    Start-Process `
        -FilePath $python `
        -ArgumentList $ArgsLine `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog

    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-WebRequest -Uri "$Url/api/status" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch {
            $ready = $false
        }
    }

    if (-not $ready) {
        Write-Host "Dashboard did not start. Check logs:"
        Write-Host $OutLog
        Write-Host $ErrLog
        Read-Host "Press Enter to close"
        exit 1
    }
}

Write-Host "NCS MCP Dashboard"
Write-Host $Url

if (-not $NoOpen) {
    Start-Process $Url
}
