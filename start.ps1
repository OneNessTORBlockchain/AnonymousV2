# Anonymous V2 - restart the API + operator UI and open it in the browser.
#
#   .\start.ps1              # restart on 127.0.0.1:8420 and open the UI
#   .\start.ps1 -NoBrowser   # restart only
#   .\start.ps1 -Port 8500   # alternative port
#   .\start.ps1 -Docker      # also start Docker Desktop + the av2-kali sandbox
#
# The FastAPI process serves both /api/* and the static frontend from
# ../frontend, so opening http://127.0.0.1:<port>/ is the whole UI.
#
# -Docker is opt-in because starting Docker Desktop boots a WSL2 VM.  Without it
# the script still restarts the API and just warns when the sandbox is down.
[CmdletBinding()]
param(
    [int]$Port = 8420,
    [switch]$NoBrowser,
    [switch]$Docker
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root 'storage\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir 'server.out.log'
$errLog = Join-Path $logDir 'server.err.log'

# --- stop a previous instance on this port (python only, never Ollama) -----
$listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'python*') {
        Write-Host "stopping previous backend (pid $($proc.Id))" -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force
        Start-Sleep -Milliseconds 800
    }
}

# --- sandbox helpers (-Docker) ----------------------------------------------
function Get-DockerServerVersion {
    # When the WSL engine is wedged the CLI prints its "unable to start" banner
    # on stderr, so only a zero exit code counts as healthy.
    $out = & docker version --format '{{.Server.Version}}' 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Out-String).Trim() }
    return $null
}

function Wait-DockerServer {
    param([int]$TimeoutSeconds = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $version = Get-DockerServerVersion
        if ($version) { return $version }
        Start-Sleep -Seconds 5
    }
    return $null
}

function Start-DockerSandbox {
    $version = Get-DockerServerVersion
    if (-not $version) {
        $exe = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
            'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $exe) {
            Write-Host 'Docker Desktop is not installed in a known location' -ForegroundColor Yellow
            return $false
        }
        Write-Host 'starting Docker Desktop' -ForegroundColor Yellow
        Start-Process -FilePath $exe | Out-Null
        $version = Wait-DockerServer -TimeoutSeconds 120
        if (-not $version) {
            # A half-bootstrapped WSL engine (wsl-bootstrap exits 0x40010004) never
            # recovers by itself: clear the processes and reset the WSL VM once.
            Write-Host 'engine did not come up, resetting WSL and retrying' -ForegroundColor Yellow
            Get-Process -Name 'Docker Desktop', 'com.docker.backend', 'com.docker.build' -ErrorAction SilentlyContinue |
                Stop-Process -Force
            Start-Sleep -Seconds 3
            & wsl.exe --shutdown | Out-Null
            Start-Sleep -Seconds 5
            Start-Process -FilePath $exe | Out-Null
            $version = Wait-DockerServer -TimeoutSeconds 180
        }
        if (-not $version) {
            Write-Host 'Docker engine still unreachable' -ForegroundColor Red
            return $false
        }
    }
    Write-Host "docker engine $version" -ForegroundColor Cyan

    $composeFile = Join-Path $root 'docker\docker-compose.yml'
    $state = (& docker inspect -f '{{.State.Status}}' av2-kali 2>$null | Out-String).Trim()
    $inspectCode = $LASTEXITCODE
    $torState = (& docker inspect -f '{{.State.Status}}' av2-tor 2>$null | Out-String).Trim()
    $torCode = $LASTEXITCODE
    if ($inspectCode -ne 0 -or $torCode -ne 0) {
        Write-Host 'creating av2-kali + av2-tor from docker/docker-compose.yml' -ForegroundColor Yellow
        & docker compose -f $composeFile up -d
    } else {
        if ($state -ne 'running') {
            Write-Host 'starting existing av2-kali container' -ForegroundColor Yellow
            & docker start av2-kali | Out-Null
        }
        if ($torState -ne 'running') {
            Write-Host 'starting existing av2-tor proxy container' -ForegroundColor Yellow
            & docker start av2-tor | Out-Null
        }
    }
    return $true
}

if ($Docker) {
    Write-Host '--- sandbox preflight ---' -ForegroundColor Cyan
    Start-DockerSandbox | Out-Null
}

# --- start the API ---------------------------------------------------------
$python = (Get-Command python).Source
$args = @(
    '-m', 'uvicorn', 'app.main:app',
    '--host', '127.0.0.1',
    '--port', "$Port",
    '--app-dir', 'backend'
)
$proc = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
    -WindowStyle Hidden -PassThru
Write-Host "started backend pid $($proc.Id) -> http://127.0.0.1:$Port/" -ForegroundColor Cyan

# --- wait for /api/health --------------------------------------------------
$deadline = (Get-Date).AddSeconds(90)
$health = $null
do {
    Start-Sleep -Seconds 2
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 5
    } catch {
        $health = $null
    }
} while (-not $health -and (Get-Date) -lt $deadline)

if (-not $health) {
    Write-Host 'backend did not answer /api/health:' -ForegroundColor Red
    Get-Content $errLog -Tail 40 -ErrorAction SilentlyContinue
    exit 1
}

$health | Select-Object status, version, provider, ollama, sandbox, planner_model,
    planner_tools, godmode, godmode_skill, transaction_gate, scope_count, egress |
    Format-List

# --- egress identity: prove the sandbox is not this device -------------------
Write-Host '--- egress identity ---' -ForegroundColor Cyan
try {
    $egress = Invoke-RestMethod "http://127.0.0.1:$Port/api/egress" -TimeoutSec 120
    $geo = $egress.geo
    $where = (@($geo.city, $geo.region, $geo.country) | Where-Object { $_ }) -join ', '
    Write-Host ("sandbox {0} ({1}; {2}) mode={3}" -f $egress.ip, $where, $geo.org, $egress.mode)
    Write-Host ("host    {0}" -f $egress.host_ip)
    if ($egress.distinct) {
        Write-Host 'distinct: yes - targets see a Tor exit node, not this device or city' -ForegroundColor Green
    } else {
        Write-Host 'distinct: NO - sandbox traffic is not proxied' -ForegroundColor Red
        Write-Host '  fix: docker start av2-tor    (or clear AV2_SANDBOX_EGRESS=direct)' -ForegroundColor Yellow
    }
} catch {
    Write-Host "egress probe failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

if ($health.sandbox -eq $false) {
    Write-Host "sandbox is not available: $($health.sandbox_detail)" -ForegroundColor Yellow
    Write-Host '  fix: .\start.ps1 -Docker    (starts Docker Desktop and the av2-kali container)' -ForegroundColor Yellow
    Write-Host '  if the engine will not bootstrap: wsl --shutdown, then relaunch Docker Desktop' -ForegroundColor Yellow
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:$Port/"
}
