# Anonymous V2 - point the backend at Venice AI, with both cloud models wired.
#
#   .\tools\connect_venice.ps1                                  # show the wiring, write nothing
#   .\tools\connect_venice.ps1 -Apply                           # write .env (backed up first)
#   .\tools\connect_venice.ps1 -Apply -Verify                   # ... and prove both models answer
#   .\tools\connect_venice.ps1 -ApiKey 'venice-...' -Apply -Verify
#
# Why this exists
# ---------------
# `.env` kept drifting back to the self-hosted Render gate (a 12c-24g CPU box
# serving a GLM GGUF behind nginx).  That gate answered a swarm with
# "HTTP 400: request (4775 tokens) exceeds the available context size (4096
# tokens)" and cost a flat monthly fee whether it answered or not, so it was
# retired on 23 Nov 2026 - tools\render\retire_glm_gate.ps1 deletes the services.
#
# Venice is OpenAI-compatible, so this script only writes settings:
#   AV2_LLM_PROVIDER=venice              -> Settings.openai_style True
#   AV2_OLLAMA_BASE_URL=https://api.venice.ai/api/v1
#   AV2_PLANNER_MODEL/WORKER_MODEL      -> glm-5-3-flash-uncensored (1M ctx, tools)
#   AV2_FLEET_MODELS                    -> GLM-5.3 + DeepSeek-V4.1, 3 lanes each
#   AV2_*_NUM_CTX                       -> the prompt ceiling, see config.py
#
# No Python changes are needed: the same OllamaClient talks /models and
# /chat/completions whenever Settings.openai_style is true.
#
# -Apply writes the repository .env after backing it up to .env.bak-<utc>; every
# key that is not listed below is left exactly as it is.  Native-only knobs
# (AV2_PLANNER_THINK, or a reasoning tier you asked to drop) are removed, because
# on this protocol they are decoration - and a decoration that reads as intent is
# how the last mis-wiring went unnoticed.
[CmdletBinding()]
param(
    # Falls back to $env:AV2_LLM_API_KEY, then to the key already in .env.
    [string]$ApiKey = '',
    [string]$BaseUrl = 'https://api.venice.ai/api/v1',
    # Planner and worker: GLM-5.3 Flash Uncensored is the cheaper of the two
    # (0.165 USD/M in, 0.55 USD/M out) and the worker only compresses tool output.
    [string]$PlannerModel = 'glm-5-3-flash-uncensored',
    [string]$WorkerModel = 'glm-5-3-flash-uncensored',
    # The fleet fans out over both models: three lanes each, same task, one ledger.
    [string[]]$FleetModels = @('glm-5-3-flash-uncensored', 'deepseek-v4-1-flash-1017-uncensored'),
    [int]$AgentsPerModel = 3,
    # Send ceiling, not the server window (Venice serves 1M tokens on both models).
    [int]$NumCtx = 131072,
    # GLM-5.3 offers low|high|max and DeepSeek-V4.1 adds none, so 'low' is the
    # cheapest value both accept.  '' removes the line and Venice defaults to high.
    [ValidateSet('', 'none', 'low', 'medium', 'high', 'max')]
    [string]$ReasoningEffort = 'low',
    [switch]$Apply,
    [switch]$Verify
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root '.env'

function Read-EnvValue {
    param([Parameter(Mandatory)][string]$Key)
    if (-not (Test-Path -LiteralPath $envPath)) { return $null }
    $pattern = '^\s*#?\s*' + [regex]::Escape($Key) + '\s*=(.*)$'
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match $pattern) {
            $value = $Matches[1].Trim()
            if ($value) { return $value }
        }
    }
    return $null
}

if (-not $ApiKey) { $ApiKey = $env:AV2_LLM_API_KEY }
if (-not $ApiKey) { $ApiKey = Read-EnvValue -Key 'AV2_LLM_API_KEY' }
if (-not $ApiKey) {
    throw ('No Venice key. Pass -ApiKey, set $env:AV2_LLM_API_KEY, or put AV2_LLM_API_KEY in .env ' +
           '(mint one at https://venice.ai/settings/api).')
}
# Venice keys are 59 characters.  The 48-hex value that used to sit in .env was
# the Render gate's bearer token: it is not a Venice key and answers 401.
if ($ApiKey.Length -lt 40) {
    throw "That does not look like a Venice key ($($ApiKey.Length) chars, expected 59): the 48-hex Render gate token is not one."
}
if ($NumCtx -lt 65536) {
    Write-Host ("note: -NumCtx {0} is below the 128k default; harmless, but a 4k window is what broke the swarm." -f $NumCtx) -ForegroundColor Yellow
}

$settings = [ordered]@{
    AV2_LLM_PROVIDER           = 'venice'
    AV2_OLLAMA_BASE_URL        = $BaseUrl.TrimEnd('/')
    AV2_LLM_API_KEY            = $ApiKey
    AV2_PLANNER_MODEL          = $PlannerModel
    AV2_WORKER_MODEL           = $WorkerModel
    AV2_FLEET_MODELS           = ($FleetModels -join ',')
    AV2_FLEET_AGENTS_PER_MODEL = "$AgentsPerModel"
    AV2_PLANNER_NUM_CTX        = "$NumCtx"
    AV2_WORKER_NUM_CTX         = "$NumCtx"
}
$dropKeys = @('AV2_PLANNER_THINK')
if ($ReasoningEffort) {
    $settings['AV2_LLM_REASONING_EFFORT'] = $ReasoningEffort
} else {
    $dropKeys += 'AV2_LLM_REASONING_EFFORT'
}

$masked = if ($ApiKey.Length -gt 8) { $ApiKey.Substring(0, 4) + '...' + $ApiKey.Substring($ApiKey.Length - 4) } else { '<unset>' }

Write-Host ''
Write-Host 'AV2 -> Venice AI cloud' -ForegroundColor Cyan
foreach ($name in $settings.Keys) {
    $value = $settings[$name]
    if ($name -eq 'AV2_LLM_API_KEY') { $value = "$masked ($($ApiKey.Length) chars)" }
    Write-Host ("  {0,-27} = {1}" -f $name, $value)
}
Write-Host ("  fleet lane count          = {0} models x {1} agents = {2}" -f $FleetModels.Count, $AgentsPerModel, ($FleetModels.Count * $AgentsPerModel))
if ($dropKeys.Count -gt 0) {
    Write-Host ("  removed on -Apply         = {0}" -f ($dropKeys -join ', ')) -ForegroundColor DarkGray
}

if (-not $Apply) {
    Write-Host ''
    Write-Host 'Dry run: nothing written.  Re-run with -Apply (and -Verify) to rewire .env.' -ForegroundColor DarkGray
    return
}


if (Test-Path -LiteralPath $envPath) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $backup = "$envPath.bak-$stamp"
    Copy-Item -LiteralPath $envPath -Destination $backup
    Write-Host ''
    Write-Host "backed up .env -> $backup" -ForegroundColor DarkGray
    $lines = @(Get-Content -LiteralPath $envPath)
} else {
    $lines = @()
}

foreach ($name in $settings.Keys) {
    $pattern = '^\s*#?\s*' + [regex]::Escape($name) + '\s*='
    $replacement = "$name=$($settings[$name])"
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) { $lines[$i] = $replacement; $found = $true; break }
    }
    if (-not $found) { $lines += $replacement }
}
if ($dropKeys.Count -gt 0) {
    $lines = @($lines | Where-Object {
        $line = $_
        -not ($dropKeys | Where-Object { $line -match ('^\s*' + [regex]::Escape($_) + '\s*=') })
    })
}
# Write without a BOM: python-dotenv reads plain UTF-8, and a BOM would land on
# whichever key happens to be first in the file.
[IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "wrote $envPath" -ForegroundColor Green

# ---------------------------------------------------------------- verify -----
if ($Verify) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) { throw 'curl.exe is required for -Verify (it ships with Windows 10+).' }
    $endpoint = ($BaseUrl.TrimEnd('/')) + '/chat/completions'
    $targets = @(@($PlannerModel) + @($FleetModels) | Select-Object -Unique)
    $failed = 0
    Write-Host ''
    Write-Host "proving inference on $endpoint" -ForegroundColor Cyan
    foreach ($model in $targets) {
        $body = [ordered]@{
            model      = $model
            messages   = @(@{ role = 'user'; content = 'Reply with the single word: ready' })
            max_tokens = 16
            stream     = $false
        } | ConvertTo-Json -Depth 5 -Compress
        $bodyFile = Join-Path $env:TEMP ('av2-venice-{0}.json' -f [guid]::NewGuid().ToString('N'))
        $outFile = "$bodyFile.out"
        [IO.File]::WriteAllText($bodyFile, $body, (New-Object System.Text.UTF8Encoding($false)))
        $curlArgs = @('-s', '-m', '120', '-o', $outFile, '-w', '%{http_code}',
            '-H', "Authorization: Bearer $ApiKey", '-H', 'Content-Type: application/json',
            '--data-binary', "@$bodyFile", $endpoint)
        $code = (& $curl.Source @curlArgs 2>$null | Out-String).Trim()
        $text = if (Test-Path -LiteralPath $outFile) { (Get-Content -LiteralPath $outFile -Raw).Trim() } else { '' }
        Remove-Item -LiteralPath $bodyFile, $outFile -ErrorAction SilentlyContinue
        $short = if ($text.Length -gt 200) { $text.Substring(0, 200) } else { $text }
        if ($code -eq '200') {
            Write-Host ("  {0,-42} HTTP 200  {1}" -f $model, $short) -ForegroundColor Green
            continue
        }
        $failed++
        Write-Host ("  {0,-42} HTTP {1}  {2}" -f $model, $code, $short) -ForegroundColor Red
        switch ($code) {
            '401' { Write-Host '       the key was rejected: mint a new one at https://venice.ai/settings/api' -ForegroundColor Yellow }
            '402' { Write-Host '       the key is valid, the account has no credit: top up, then re-run -Verify' -ForegroundColor Yellow }
            '404' { Write-Host '       unknown model id: GET /api/v1/models lists the current ids' -ForegroundColor Yellow }
            '429' { Write-Host '       rate limit: lower -AgentsPerModel or wait' -ForegroundColor Yellow }
            default { Write-Host '       unexpected answer; the body above is the provider''s own sentence' -ForegroundColor Yellow }
        }
    }
    if ($failed -gt 0) { throw "$failed of $($targets.Count) model(s) did not answer; .env is wired but AV2 cannot infer yet." }
    Write-Host 'both models answered: AV2 is wired to Venice and nothing else' -ForegroundColor Green
}

Write-Host ''
Write-Host 'Restart and check: .\start.ps1 ; GET /api/health -> provider=venice ollama=True' -ForegroundColor DarkGray
