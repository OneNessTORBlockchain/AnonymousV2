# Anonymous V2 - point the backend at a self-hosted llama.cpp / Ollama endpoint.
#
#   .\connect_av2.ps1 -BaseUrl https://203.0.113.7/v1 -ApiKey 'av2_...'          # print only
#   .\connect_av2.ps1 -BaseUrl https://203.0.113.7/v1 -ApiKey 'av2_...' -Apply   # write .env
#   .\connect_av2.ps1 -BaseUrl https://glm.example.com/v1 -Apply -CaCert C:\certs\root.crt
#   .\connect_av2.ps1 -BaseUrl http://127.0.0.1:8080/v1 -Model 'glm-5.3:cloud' -NoAuth -Apply
#   .\connect_av2.ps1 -BaseUrl http://127.0.0.1:11434 -Model '<tag>' -Native -NumCtx 16384 -Apply
#
# The cloud default is **not** this: `.env` points at Venice AI
# (https://api.venice.ai/api/v1, 1M-token window), and tools\connect_venice.ps1 is
# the script that keeps it there.  This one exists for a GPU box or a local
# daemon, i.e. the case where the 4 096-token default window is too small.
#
# -Native is the context-window lever on such a box.  Only /api/chat carries
# options.num_ctx, so it is the only way to run above the window the daemon booted
# with (4096 unless OLLAMA_CONTEXT_LENGTH says otherwise) without touching the box
# -- and 4096 is smaller than one AV2 system prompt, which is what kills a swarm
# with "exceeds the available context size" (docs/CONTEXT-WINDOW.md).  The Render
# gate that hosted exactly such a box was retired on 23 Nov 2026: docs/STATUS.md,
# tools\render\retire_glm_gate.ps1.
#
# No Python changes are needed: Settings.openai_style is already true whenever
# AV2_LLM_PROVIDER is one of venice | openai | openai-compatible
# (backend/app/config.py:openai_style), so OllamaClient switches to /models and
# /chat/completions and starts sending "Authorization: Bearer <AV2_LLM_API_KEY>".
#
# -Apply writes the repository .env after backing it up to .env.bak-<utc>; keys
# that are not AV2_* are left exactly as they are.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BaseUrl,
    [string]$ApiKey = '',
    [string]$Model = 'glm-5.3-flash-uncensored-iq1s',
    [string]$CaCert = '',
    # OpenAI-compatible endpoints only: 'none' disables chain-of-thought, which is
    # what a thinking model on a CPU box needs (see config.py:llm_reasoning_effort).
    # Ollama's /v1 shim ignores the native `think` flag and obeys this one, so
    # leaving it unset against that endpoint returns empty content, not answers.
    [ValidateSet('', 'none', 'low', 'medium', 'high')]
    [string]$ReasoningEffort = '',
    # Talk the native Ollama protocol (POST /api/chat) instead of /v1/chat/completions.
    # It is the only surface that carries ``options.num_ctx``, so it is the only way to
    # raise the context window per request instead of restarting the daemon with
    # OLLAMA_CONTEXT_LENGTH.
    [switch]$Native,
    # Context window requested on every call.  Must fit the inference host: the
    # deepseek2 29.9B model costs ~320 KB of KV cache per token on the 24 GB brain, so
    # 16 k is ~5 GB on top of 10.3 GB of weights and 32 k is ~10 GB (measured: 20.1 GiB
    # resident at 32 k, which starves the page cache - docs/CONTEXT-WINDOW.md).
    [int]$NumCtx = 16384,
    # Worker (summariser) window.  0 = same as -NumCtx, which is what you want against
    # one daemon: Ollama reloads the runner whenever num_ctx changes.
    [int]$WorkerNumCtx = 0,
    # Loopback Ollama needs no bearer token; every remote box does (llama-server is
    # started with --api-key in tools/glm53/cloud-init.yaml).
    [switch]$NoAuth,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$envPath = Join-Path $root '.env'

$trimmed = $BaseUrl.TrimEnd('/')
if ($Native) {
    if ($trimmed -match '/v1$') {
        $trimmed = $trimmed.Substring(0, $trimmed.Length - 3)
        Write-Host "note: -Native drops the trailing /v1; AV2 will POST $trimmed/api/chat." -ForegroundColor Yellow
    }
    if ($ReasoningEffort) {
        Write-Host "note: -ReasoningEffort only exists on the OpenAI surface; native uses AV2_PLANNER_THINK=false." -ForegroundColor Yellow
    }
} elseif ($trimmed -notmatch '/v1$') {
    Write-Host "note: llama-server exposes the OpenAI surface under /v1; '$BaseUrl' may need it appended." -ForegroundColor Yellow
}
if ($NumCtx -lt 2048) {
    throw "-NumCtx $NumCtx is smaller than one AV2 system prompt (~4.7 k tokens); pick at least 8192."
}
if (-not $ApiKey -and $Apply -and -not $NoAuth) {
    throw 'Refusing to write .env without -ApiKey: llama-server was started with --api-key and will reject anonymous calls. Pass -NoAuth only for a loopback endpoint that has no --api-key.'
}
if ($ApiKey -and $NoAuth) {
    throw '-ApiKey and -NoAuth are mutually exclusive: pick one.'
}

# The server is started with -c <slot ctx x --parallel>, so the planner context
# must stay inside one slot (cloud-init.yaml: -c "$CTX" --parallel "$PARALLEL").
$key = if ($NoAuth) { '' } else { $ApiKey }
$settings = [ordered]@{
    AV2_LLM_PROVIDER  = $(if ($Native) { 'ollama' } else { 'openai' })
    AV2_OLLAMA_BASE_URL = $trimmed
    AV2_LLM_API_KEY   = $key
    AV2_PLANNER_MODEL = $Model
    AV2_WORKER_MODEL  = $Model
    AV2_PLANNER_NUM_CTX = "$NumCtx"
    AV2_WORKER_NUM_CTX = "$(if ($WorkerNumCtx -gt 0) { $WorkerNumCtx } else { $NumCtx })"
}
# A GLM-only box serves exactly one model id, but the fleet defaults to the two
# Venice ids (config.py CLOUD_MODEL + DEEPSEEK_MODEL).  Left unset, half the fleet
# is sent to a DeepSeek model the instance does not have: those lanes fail with a
# 404 while GLM answers, and every report is half-empty.  Pinning the single id
# makes the fleet 1 model x 3 agents = 3 lanes, all on GLM.
$settings['AV2_FLEET_MODELS'] = $Model
$settings['AV2_FLEET_AGENTS_PER_MODEL'] = '3'
# Only written when asked for: an empty value would be a lie about intent, and
# Venice's own default is the right one unless the operator says otherwise.
if ($Native) {
    # /api/chat reads ``think`` directly, so the OpenAI spelling is not written: a
    # thinking model left on spends the turn inside message.thinking and returns
    # empty content, which reads downstream as no answer at all.
    $settings['AV2_PLANNER_THINK'] = 'false'
} elseif ($ReasoningEffort) {
    $settings['AV2_LLM_REASONING_EFFORT'] = $ReasoningEffort
}

if ($CaCert) { $settings['SSL_CERT_FILE'] = (Resolve-Path -LiteralPath $CaCert).Path }

$masked = if ($ApiKey.Length -gt 8) { $ApiKey.Substring(0, 6) + '...' + $ApiKey.Substring($ApiKey.Length - 4) } else { '<unset>' }

Write-Host ''
Write-Host "AV2 -> self-hosted GLM-5.3 endpoint" -ForegroundColor Cyan
foreach ($key in $settings.Keys) {
    $value = $settings[$key]
    if ($key -eq 'AV2_LLM_API_KEY') { $value = $masked }
    Write-Host ("  {0,-20} = {1}" -f $key, $value)
}
if ($CaCert) {
    Write-Host ''
    Write-Host 'SSL_CERT_FILE matters for the internal-CA case: httpx verifies TLS by default,' -ForegroundColor DarkGray
    Write-Host 'and it honours SSL_CERT_FILE, so pointing it at Caddy root.crt is what makes' -ForegroundColor DarkGray
    Write-Host 'https://<ip>/v1 work without disabling verification.' -ForegroundColor DarkGray
}

if (-not $Apply) {
    Write-Host ''
    Write-Host 'Dry run: nothing written.  Re-run with -Apply to update .env, then .\start.ps1' -ForegroundColor DarkGray
    Write-Host 'and check GET /api/health for provider=openai and ollama=ok.' -ForegroundColor DarkGray
    return
}

if (Test-Path -LiteralPath $envPath) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $backup = "$envPath.bak-$stamp"
    Copy-Item -LiteralPath $envPath -Destination $backup
    Write-Host "backed up .env -> $backup" -ForegroundColor DarkGray
    $lines = @(Get-Content -LiteralPath $envPath)
} else {
    $lines = @()
}

foreach ($key in $settings.Keys) {
    $value = $settings[$key]
    $pattern = "^\s*$([regex]::Escape($key))\s*="
    $replacement = "$key=$value"
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = $replacement
            $found = $true
            break
        }
    }
    if (-not $found) { $lines += $replacement }
}

Set-Content -LiteralPath $envPath -Value $lines -Encoding UTF8
Write-Host "wrote $envPath" -ForegroundColor Green
Write-Host 'Restart the backend (.\start.ps1) and confirm: GET /api/health -> provider=openai, ollama=ok' -ForegroundColor Green
Write-Host 'Then measure the model: python tools/glm53/eval_model.py --base-url <url> --api-key <key>' -ForegroundColor Green
