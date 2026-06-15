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
$DbPath = Join-Path $Root "data\processed\ncs.db"
$Logs = Join-Path $Root "logs"
$OutLog = Join-Path $Logs "dashboard.out.log"
$ErrLog = Join-Path $Logs "dashboard.err.log"
$Url = "http://$HostAddress`:$Port"

function Get-ListeningProcessId {
    param([int]$LocalPort)

    $connection = Get-NetTCPConnection -LocalPort $LocalPort -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -First 1
    if ($connection) {
        return [int]$connection.OwningProcess
    }

    $pattern = "^\s*TCP\s+\S+:$LocalPort\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($line in netstat -ano) {
        if ($line -match $pattern) {
            return [int]$Matches[1]
        }
    }

    return $null
}

New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$env:PYTHONPATH = $PythonPath
$env:NCS_DB_PATH = $DbPath

$existingPid = Get-ListeningProcessId -LocalPort $Port

if ($Restart -and $existingPid) {
    Stop-Process -Id $existingPid -Force
    Start-Sleep -Seconds 1
    $existingPid = Get-ListeningProcessId -LocalPort $Port
}

if (-not $existingPid) {
    $python = (Get-Command python -ErrorAction Stop).Source
    $ArgsLine = "-u `"$Dashboard`" --host `"$HostAddress`" --port $Port --db-path `"$DbPath`""
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
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
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
