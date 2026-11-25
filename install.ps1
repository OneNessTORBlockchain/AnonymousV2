# Anonymous V2 - one-shot dependency installer (Windows / PowerShell).
#
#   .\install.ps1                  # Python packages (+pytest) and a readiness report
#   .\install.ps1 -Docker         # also start Docker Desktop and pull the Tor sidecar
#   .\install.ps1 -InstallDocker  # install Docker Desktop through winget when absent
#   .\install.ps1 -Upgrade        # pip install --upgrade instead of a plain install
#
# There is no requirements.txt in the tree: this script *is* the list, so a fresh
# clone can go from nothing to ".\start.ps1" with one command.  The Kali worker
# image (oneness-kali-agent:local) is purpose-built and is not built here - the
# script verifies it and prints the exact command when it is missing.
[CmdletBinding()]
param(
    [switch]$Docker,
    [switch]$InstallDocker,
    [switch]$Upgrade
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step { param([string]$Text) Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok { param([string]$Text) Write-Host "   ok  $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "   !!  $Text" -ForegroundColor Yellow }

# --- python ----------------------------------------------------------------
Write-Step 'python'
$script:pyExe = $null
$script:pyBase = @()
foreach ($candidate in @('python', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($candidate -eq 'py') {
        $probe = & py -3 -c "import sys;print(sys.version.split()[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) {
            $script:pyExe = 'py'
            $script:pyBase = @('-3')
            Write-Ok "py -3 $probe"
            break
        }
    } else {
        $probe = & python -c "import sys;print(sys.version.split()[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) {
            $script:pyExe = $cmd.Source
            $script:pyBase = @()
            Write-Ok "python $probe"
            break
        }
    }
}
if (-not $script:pyExe) {
    Write-Warn 'Python 3.10+ was not found.'
    Write-Warn 'install it with:  winget install -e --id Python.Python.3.11'
    Write-Warn 'or from https://www.python.org/downloads/ (tick "Add python.exe to PATH")'
    exit 1
}

function Invoke-Py {
    param([string[]]$Arguments)
    & $script:pyExe @($script:pyBase + $Arguments)
}

# --- python packages -------------------------------------------------------
Write-Step 'python packages'
$packages = @('fastapi', 'uvicorn[standard]', 'httpx', 'pydantic-settings')
$devPackages = @('pytest', 'pytest-asyncio', 'pytest-xprocess')
$pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check')
if ($Upgrade) { $pipArgs += '--upgrade' }
Invoke-Py (@($pipArgs) + $packages + $devPackages)
if ($LASTEXITCODE -ne 0) {
    Write-Warn 'pip failed. If this machine needs an index or a proxy, set PIP_INDEX_URL and retry.'
    exit 1
}
Invoke-Py @(
    '-c',
    'import fastapi, uvicorn, httpx, pydantic_settings; print("fastapi", fastapi.__version__, "| httpx", httpx.__version__)'
)
if ($LASTEXITCODE -ne 0) {
    Write-Warn 'the imports still fail after installing - check for a second Python on PATH.'
    exit 1
}
Write-Ok 'fastapi, uvicorn, httpx, pydantic-settings import cleanly'

# --- repository .env -------------------------------------------------------
Write-Step 'configuration'
$envFile = Join-Path $root '.env'
$example = Join-Path $root '.env.example'
if (Test-Path $envFile) {
    Write-Ok '.env present'
} elseif (Test-Path $example) {
    Copy-Item -LiteralPath $example -Destination $envFile
    Write-Ok '.env created from .env.example'
    Write-Warn 'paste your key into AV2_OLLAMA_API_KEY (or use the Provider panel in the UI)'
} else {
    Write-Warn '.env is missing and there is no .env.example - write your endpoint/key before starting.'
}


# --- docker ----------------------------------------------------------------
Write-Step 'docker'
$kaliImage = 'oneness-kali-agent:local'
$torImage = 'dperson/torproxy:latest'

function Get-DockerServerVersion {
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

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Warn 'Docker Desktop is not installed - the Kali sandbox needs it.'
    if ($InstallDocker -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Step 'installing Docker Desktop (winget)'
        & winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Warn 'winget could not install it; get it from https://www.docker.com/products/docker-desktop/'
        } else {
            Write-Ok 'Docker Desktop installed - sign out and back in if "docker" is still not on PATH'
        }
    } else {
        Write-Warn 'install it with:  winget install -e --id Docker.DockerDesktop'
        Write-Warn 'or from https://www.docker.com/products/docker-desktop/'
    }
} else {
    $version = Get-DockerServerVersion
    if (-not $version -and $Docker) {
        $exe = @(
            (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'),
            'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($exe) {
            Write-Host '   starting Docker Desktop...'
            Start-Process -FilePath $exe | Out-Null
            $version = Wait-DockerServer -TimeoutSeconds 180
        }
    }
    if ($version) {
        Write-Ok "docker server $version"
        & docker image inspect $kaliImage *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "image $kaliImage"
        } else {
            Write-Warn "image $kaliImage is missing. Build or import the oneness-kali-agent worker, then run:"
            Write-Warn '   docker compose -f docker/docker-compose.yml up -d'
        }
        if ($Docker) {
            Write-Host "   pulling $torImage..."
            & docker pull $torImage
            if ($LASTEXITCODE -eq 0) { Write-Ok "image $torImage" } else { Write-Warn "could not pull $torImage" }
        }
    } else {
        Write-Warn 'Docker is installed but its engine is not running (start Docker Desktop, or rerun with -Docker).'
    }
}

# --- done ------------------------------------------------------------------
Write-Step 'next'
Write-Host '   .\start.ps1            # API + UI on http://127.0.0.1:8420'
Write-Host '   .\start.ps1 -Docker    # same, plus the av2-kali sandbox'
Write-Host '   cd backend; python -m pytest -q    # check the tree'
