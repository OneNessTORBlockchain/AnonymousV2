# Anonymous V2 - the whole GLM go-live, in the order that actually works.
#
#   .\go_live.ps1                                     # plans only, spends nothing
#   .\go_live.ps1 -Execute -ApproveSpendUsd 96 `
#       -OperatorCidr 203.0.113.7/32 -Owner 'Kathy' `
#       -AuthorizationRef 'AV2-ENG-2026-11' -Targets '10.0.0.252:8081-8083' `
#       -Production
#
# Stages, and why each one exists:
#   1. spend gate      cost = hourly x BurstHours, refused unless inside
#                      -ApproveSpendUsd (the same number the provisioner's own
#                      budget gate is given: one number, one decision).
#   2. provision       delegates to provision_akamai.ps1 -Execute: cloud-init builds
#                      CUDA llama.cpp, fetches the 71.5 GiB IQ1_S and serves /v1
#                      behind Caddy with a generated bearer token.
#   3. discover        reads instance id/IP back from the API by label, so the
#                      teardown command printed at the end is the right one.
#   4. credentials     reads API_KEY from /etc/av2-glm53/env over SSH and the Caddy
#                      internal root CA over SCP. The key never touches the CLI.
#   5. wire AV2        connect_av2.ps1 -Apply: provider=openai and ONE model id for
#                      planner, worker and fleet (a GLM-only box has no DeepSeek
#                      lane for the other half of AV2's default fleet).
#   6. restart+assert  .\start.ps1, then /api/health must report provider=openai,
#                      ollama=true and fleet_models = that single id.
#   7. prove inference a real /v1/chat/completions POST against the box: a
#                      deployment that only answers /v1/models is a billing script.
#   8. engagement      PUT /api/engagement with owner, authorization_ref and the
#                      targets, then AV2_ENVIRONMENT=production, then assert
#                      mode=production and contract_problems=[]. Without a contract,
#                      production refuses *every* command (production.py
#                      GuardrailEngine.evaluate), so contract first, flag second.
#   9. measure         eval_model.py --suite tools, saved to JSON.
#
# Nothing here deletes anything; the box is only created by the explicit -Execute
# path. The last line printed is the command that stops the meter.
[CmdletBinding()]
param(
    # 4 x RTX 6000 Ada = 96 GB VRAM + 128 GB RAM, $6.00/h: the smallest Akamai shape
    # that holds IQ1_S fully resident (provision_akamai.ps1 -List, 2026-11-22).
    [string]$Plan = 'g1-gpu-rtx6000-4',
    [string]$Region = 'us-east',
    [string]$Image = 'linode/ubuntu24.04',
    [double]$BurstHours = 16,
    [string]$OperatorCidr = '',
    [string]$Domain = '',
    [string]$Model = 'glm-5.3-flash-uncensored-iq1s',

    # llama-server's -c is shared by the --parallel slots, so one AV2 planner turn
    # needs -c >= slot ctx x parallel. 2 x 16 k = 32 k keeps the 16 k planner
    # context inside a single slot; --parallel 4 would leave 4 k per slot.
    [int]$SlotCtx = 16384,
    [int]$Parallel = 2,

    # Existing box: skip provisioning and use this IP + token instead.
    [switch]$SkipProvision,
    [string]$Ip = '',
    [string]$ApiKey = '',

    # Production engagement (owner, ref and targets are required with -Production).
    [switch]$Production,
    [string]$Owner = '',
    [string]$AuthorizationRef = '',
    [string[]]$Targets = @(),
    [string[]]$Actions = @('recon', 'vulnerability-scan', 'exploit-validation', 'auth-testing', 'file-read'),
    [string]$EngagementName = '',
    [int]$RateLimitPerMinute = 60,
    [int]$MaxCommands = 0,
    [double]$ContractHours = 24,

    [int]$Port = 8420,
    [int]$WaitMinutes = 45,
    [double]$ApproveSpendUsd = 0,
    [switch]$SkipEval,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$here = $PSScriptRoot
$root = Split-Path -Parent (Split-Path -Parent $here)
$provision = Join-Path $here 'provision_akamai.ps1'
$connect = Join-Path $here 'connect_av2.ps1'
$startScript = Join-Path $root 'start.ps1'
$envPath = Join-Path $root '.env'
$base = "http://127.0.0.1:$Port"
$api = 'https://api.linode.com/v4'
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

function Write-Stage { param([string]$Text) Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Step { param([string]$Text) Write-Host "   $Text" }
function Write-Warn { param([string]$Text) Write-Host "   $Text" -ForegroundColor Yellow }
function Write-Ok { param([string]$Text) Write-Host "   $Text" -ForegroundColor Green }

function Get-PlanPrice {
    param([Parameter(Mandatory)][string]$Id)
    $types = (Invoke-RestMethod -Uri "$api/linode/types" -TimeoutSec 60).data
    $type = $types | Where-Object { $_.id -eq $Id } | Select-Object -First 1
    if (-not $type) { throw "unknown Linode type '$Id' (provision_akamai.ps1 -List shows the GPU ids)" }
    $perCard = if ($type.id -match 'rtx4000') { 20 } else { 24 }
    return [pscustomobject]@{
        Id     = $type.id
        Gpus   = [int]$type.gpus
        VramGb = [int]$type.gpus * $perCard
        RamGb  = [int]($type.memory / 1024)
        Hourly = [double]$type.price.hourly
    }
}

function Get-EnvValue {
    param([Parameter(Mandatory)][string]$Key)
    if (-not (Test-Path -LiteralPath $envPath)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match "^\s*$([regex]::Escape($Key))\s*=(.*)$") { return $Matches[1].Trim() }
    }
    return $null
}
function Set-EnvValue {
    param([Parameter(Mandatory)][string]$Key, [string]$Value)
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    if (Test-Path -LiteralPath $envPath) {
        Copy-Item -LiteralPath $envPath -Destination "$envPath.bak-$stamp"
        $lines = @(Get-Content -LiteralPath $envPath)
    } else {
        $lines = @()
    }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($Key))\s*=") { $lines[$i] = "$Key=$Value"; $found = $true; break }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    Set-Content -LiteralPath $envPath -Value $lines -Encoding UTF8
    return $stamp
}

function Wait-Health {
    param([int]$TimeoutSeconds = 150)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try { return Invoke-RestMethod "$base/api/health" -TimeoutSec 5 } catch { Start-Sleep -Seconds 3 }
    }
    return $null
}

function Restart-Av2 {
    # start.ps1 runs a Tor egress probe that can take minutes when the sandbox is
    # down, so it is launched detached and this script polls /api/health itself.
    $log = Join-Path $env:TEMP 'av2-go-live-start.log'
    $sArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startScript, '-NoBrowser', '-Port', "$Port")
    Start-Process -FilePath 'powershell.exe' -ArgumentList $sArgs -WorkingDirectory $root `
        -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err" | Out-Null
    return Wait-Health
}

# ---------------------------------------------------------------- 1. spend ------
Write-Stage '1. spend gate'
$spec = Get-PlanPrice -Id $Plan
$Ctx = $SlotCtx * $Parallel
$cost = [math]::Round($spec.Hourly * $BurstHours, 2)
Write-Step ("{0}: {1} cards, {2} GB VRAM + {3} GB RAM, `${4:0.00}/h" -f $spec.Id, $spec.Gpus, $spec.VramGb, $spec.RamGb, $spec.Hourly)
Write-Step ("{0} h x `${1:0.00}/h = `${2:0.00}    (24/7 would be `${3:0.00})" -f $BurstHours, $spec.Hourly, $cost, [math]::Round($spec.Hourly * 730, 2))
Write-Step ("llama-server: -c {0} --parallel {1}  =>  {2} tokens per slot" -f $Ctx, $Parallel, $SlotCtx)
if (($spec.VramGb - 12) -lt 71.5) {
    Write-Warn ("{0} GB VRAM cannot hold 71.5 GiB: the serve wrapper keeps MoE experts in system RAM." -f $spec.VramGb)
} else {
    Write-Ok ("{0} GB VRAM holds the 71.5 GiB fully resident (12 GB reserved for KV + CUDA)." -f $spec.VramGb)
}
if ($BurstHours -le 0) { throw '-BurstHours must be greater than 0.' }
if ($Execute -and $ApproveSpendUsd -le 0) {
    throw ("Refusing to spend without -ApproveSpendUsd: this run costs `${0}. Re-run with -ApproveSpendUsd {1}." -f $cost, [math]::Ceiling($cost))
}
if ($Execute -and $cost -gt $ApproveSpendUsd) {
    throw ("Refusing: this run costs `${0}, above the `${1} approved." -f $cost, $ApproveSpendUsd)
}
if ($Production) {
    $missing = @()
    if (-not $Owner) { $missing += '-Owner' }
    if (-not $AuthorizationRef) { $missing += '-AuthorizationRef' }
    if (-not $Targets) { $missing += '-Targets' }
    if ($missing.Count -gt 0) {
        throw "-Production needs the operator's written authorization: $($missing -join ', '). Production with no contract refuses every command."
    }
    Write-Step ("engagement : owner='{0}' ref='{1}' targets={2}" -f $Owner, $AuthorizationRef, ($Targets -join ','))
    Write-Step ("actions    : {0}  (nothing else is executable in production)" -f ($Actions -join ','))
} else {
    Write-Step 'engagement : not requested, AV2 stays in lab mode (annotate, never refuse)'
}
if (-not $Execute -and -not $SkipProvision) {
    Write-Warn '-Execute not passed: plan only. Nothing is created, nothing is spent.'
}
# ------------------------------------------------------------- 2. provision ----
Write-Stage '2. provision'
if ($SkipProvision) {
    if (-not $Ip) { throw '-SkipProvision needs -Ip <public ipv4>.' }
    Write-Step "skipping the API: using the existing box at $Ip"
} elseif (-not $Execute) {
    Write-Step ("would run: provision_akamai.ps1 -Plan {0} -Region {1} -BurstHours {2} -MonthlyBudgetUsd <cost> -OperatorCidr <ip>/32 -Ctx {3} -Parallel {4} -WaitMinutes {5} -Execute" -f `
            $Plan, $Region, $BurstHours, $Ctx, $Parallel, $WaitMinutes)
    Write-Step 'cloud-init on the box: fetch 71.5 GiB -> CUDA llama.cpp build -> Caddy -> llama-server -> /etc/av2-glm53/ready'
} else {
    if (-not $OperatorCidr) { throw '-Execute needs -OperatorCidr <your.ip>/32: 22/443 are never opened to the world.' }
    if (-not $env:LINODE_API_TOKEN) { throw 'Set $env:LINODE_API_TOKEN (read/write on Linodes only) before -Execute.' }
    $pArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $provision,
        '-Plan', $Plan, '-Region', $Region, '-Image', $Image,
        '-BurstHours', "$BurstHours", '-MonthlyBudgetUsd', "$ApproveSpendUsd",
        '-OperatorCidr', $OperatorCidr, '-Ctx', "$Ctx", '-Parallel', "$Parallel",
        '-WaitMinutes', "$WaitMinutes", '-Execute')
    if ($Domain) { $pArgs += @('-Domain', $Domain) }
    Write-Step ("provision_akamai.ps1 -Plan {0} -BurstHours {1} -Execute ..." -f $Plan, $BurstHours)
    & powershell.exe @pArgs
    if ($LASTEXITCODE -ne 0) {
        throw "provision_akamai.ps1 exited $LASTEXITCODE. If the instance was created it is still billing: provision_akamai.ps1 -Destroy -InstanceId <id>."
    }
    Write-Ok 'provisioner reported the endpoint answered'
}

# -------------------------------------------------------------- 3. discover ----
Write-Stage '3. discover the instance'
if ($Ip) {
    Write-Step "ip         : $Ip"
    $instanceId = 0
} elseif (-not $Execute) {
    Write-Step 'would read the instance id and public IPv4 from GET /linode/instances by label av2-glm53*'
    $Ip = '<instance-ip>'
    $instanceId = 0
} else {
    if (-not $env:LINODE_API_TOKEN) { throw 'Cannot read the instance list without $env:LINODE_API_TOKEN.' }
    $headers = @{ Authorization = "Bearer $($env:LINODE_API_TOKEN)"; Accept = 'application/json' }
    $all = (Invoke-RestMethod -Uri "$api/linode/instances?page_size=500" -Headers $headers -TimeoutSec 60).data
    $box = $all | Where-Object { $_.label -like 'av2-glm53*' } | Sort-Object { $_.created } -Descending | Select-Object -First 1
    if (-not $box) { throw 'No instance labelled av2-glm53* on this account. Did the provisioner create one?' }
    $instanceId = [int]$box.id
    $Ip = ($box.ipv4 | Select-Object -First 1)
    Write-Step "instance   : $instanceId [$($box.label)] $($box.status)"
    Write-Step "ip         : $Ip"
    if ($box.status -ne 'running') { Write-Warn "status is '$($box.status)', not running - the bootstrap may still be queued" }
}
$hostName = if ($Domain) { $Domain } else { $Ip }
$baseUrl = "https://$hostName/v1"

# ------------------------------------------------------------ 4. credentials ---
Write-Stage '4. credentials'
if ($ApiKey) {
    Write-Step 'api key    : supplied with -ApiKey'
} elseif ($SkipProvision -and $Execute -and -not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
    throw 'ssh.exe not found: pass -ApiKey (read it with: ssh root@<ip> "grep ^API_KEY /etc/av2-glm53/env").'
} elseif ($Execute) {
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    Write-Step "api key    : reading /etc/av2-glm53/env over ssh (BatchMode, no password prompt)"
    $line = & $ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "root@$Ip" 'grep -m1 ^API_KEY= /etc/av2-glm53/env' 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ssh to root@$Ip failed: $line" }
    $ApiKey = (($line | Out-String).Trim() -split '=', 2)[1]
    if (-not $ApiKey -or -not $ApiKey.StartsWith('av2_')) { throw "could not parse API_KEY from the box: $line" }
    Write-Ok ("api key    : {0}...{1} ({2} chars)" -f $ApiKey.Substring(0, 8), $ApiKey.Substring($ApiKey.Length - 4), $ApiKey.Length)
} else {
    Write-Step 'api key    : would be read from the box over ssh'
}
$caCert = ''
if (-not $Domain -and $Execute) {
    $scp = (Get-Command scp.exe -ErrorAction SilentlyContinue).Source
    if (-not $scp) { throw 'scp.exe not found: Caddy serves an internal CA, so the root certificate is required (or pass -Domain).' }
    $dir = Join-Path $env:LOCALAPPDATA 'AV2'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $caCert = Join-Path $dir 'caddy-root.crt'
    Write-Step 'tls        : copying the Caddy internal root CA'
    & $scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@${Ip}:/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt" $caCert
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $caCert)) { throw "could not copy the Caddy root CA; pass -Domain <name> or fetch it manually." }
    Write-Ok "tls        : $caCert"
} elseif (-not $Domain) {
    Write-Step 'tls        : would copy the Caddy internal root CA over scp'
} else {
    Write-Step 'tls        : public certificate for the domain, nothing to copy'
}
# --------------------------------------------------------------- 5. wire AV2 ---
Write-Stage '5. wire AV2'
$cArgs = @('-BaseUrl', $baseUrl, '-Model', $Model)
if ($ApiKey) { $cArgs += @('-ApiKey', $ApiKey) } else { $cArgs += '-NoAuth' }
if ($caCert) { $cArgs += @('-CaCert', $caCert) }
if ($Execute) { $cArgs += '-Apply' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $connect @cArgs
if ($LASTEXITCODE -ne 0) { throw "connect_av2.ps1 exited $LASTEXITCODE; .env was not wired." }
if ($Execute) {
    Write-Ok ("wired: provider=openai, one model id ({0}) for planner, worker and all 3 fleet lanes" -f $Model)
    Write-Step 'fleet = 1 model x 3 agents: the GLM-only shape, no DeepSeek lane is called'
}

# ---------------------------------------------------------- 6. restart+assert ---
Write-Stage '6. restart and assert /api/health'
if (-not $Execute) {
    Write-Step ("would restart .\start.ps1 -NoBrowser and require: provider=openai, ollama=true, fleet_models=[{0}]" -f $Model)
} else {
    $health = Restart-Av2
    if (-not $health) { throw 'the backend did not answer /api/health after the restart; see storage\logs\server.err.log' }
    Write-Step ("provider={0} ollama={1} planner={2} tools={3} sandbox={4}" -f $health.provider, $health.ollama, $health.planner_model, $health.planner_tools, $health.sandbox)
    Write-Step ("fleet_models={0} fleet_size={1}" -f ($health.fleet_models -join ','), $health.fleet_size)
    if ($health.provider -ne 'openai') { throw "AV2 is still on provider '$($health.provider)' - the .env write did not take effect." }
    if (-not $health.ollama) { throw "the endpoint did not answer /models: $($health.ollama_detail)" }
    if (-not $health.planner_tools) { throw 'planner_tools=false: the model id does not support tool calling.' }
    $served = @($health.fleet_models)
    if ($served.Count -ne 1 -or $served[0] -ne $Model) {
        throw "fleet_models is $($served -join ',') but the box serves only '$Model'. Run connect_av2.ps1 -Apply again."
    }
    if (-not $health.sandbox) { Write-Warn "sandbox unavailable: $($health.sandbox_detail) - start it with .\start.ps1 -Docker" }
    Write-Ok 'AV2 is pointing at the box and nothing else'
}

# ---------------------------------------------------------- 7. prove inference --
Write-Stage '7. prove inference (not just /models)'
$prompt = '{"model":"' + $Model + '","messages":[{"role":"user","content":"Reply with exactly: AV2 LIVE"}],"max_tokens":16,"stream":false}'
$chatFile = Join-Path $env:TEMP ('av2-golive-{0}.json' -f [guid]::NewGuid().ToString('N'))
$outFile = "$chatFile.out"
[IO.File]::WriteAllText($chatFile, $prompt, (New-Object Text.UTF8Encoding($false)))
if (-not $Execute) {
    Write-Step "would POST $baseUrl/chat/completions with the generated token and require HTTP 200 + non-empty content"
} else {
    $curl = (Get-Command curl.exe -ErrorAction Stop).Source
    $curlArgs = @('-s', '-m', '180', '-o', $outFile, '-w', '%{http_code}', '-H', "Authorization: Bearer $ApiKey", '-H', 'Content-Type: application/json', '--data-binary', "@$chatFile")
    if ($caCert) { $curlArgs += @('--cacert', $caCert) } else { $curlArgs += '-k' }
    $code = & $curl @curlArgs "$baseUrl/chat/completions" 2>$null
    $body_ = if (Test-Path -LiteralPath $outFile) { Get-Content -LiteralPath $outFile -Raw } else { '' }
    Remove-Item -LiteralPath $chatFile, $outFile -ErrorAction SilentlyContinue
    if ($code -ne '200') { throw "the box answered HTTP $code to a real completion: $body_" }
    try { $json = $body_ | ConvertFrom-Json } catch { throw "the box returned 200 but not JSON: $body_" }
    $answer = [string]$json.choices[0].message.content
    if (-not $answer.Trim()) { throw "the box returned an empty completion: $body_" }
    Write-Ok ("inference ok: '{0}'" -f $answer.Trim().Substring(0, [math]::Min(60, $answer.Trim().Length)))
}
# ------------------------------------------------------------ 8. engagement ---
Write-Stage '8. engagement contract, then production mode'
$payload = [ordered]@{
    engagement            = if ($EngagementName) { $EngagementName } else { 'av2-glm53-' + (Get-Date -Format 'yyyyMMdd') }
    owner                 = $Owner
    authorization_ref     = $AuthorizationRef
    allowed_targets       = @($Targets)
    allowed_actions       = @($Actions)
    excluded_targets      = @()
    expires_at            = (Get-Date).ToUniversalTime().AddHours($ContractHours).ToString('yyyy-MM-ddTHH:mm:ssZ')
    rate_limit_per_minute = $RateLimitPerMinute
    max_commands          = $MaxCommands
    dry_run               = $false
    kill_switch           = $false
    notes                 = "Written authorization for $Owner, ref $AuthorizationRef. Targets are the operator's own systems."
}
if (-not $Production) {
    Write-Step 'not requested: AV2 stays in lab mode (commands annotated, never refused)'
} elseif (-not $Execute) {
    Write-Step ("would PUT {0}/api/engagement with owner='{1}' targets={2} expires in {3} h" -f $base, $Owner, ($Targets -join ','), $ContractHours)
    Write-Step 'then AV2_ENVIRONMENT=production in .env, restart, and require mode=production + contract_problems=[]'
} else {
    $json = $payload | ConvertTo-Json -Depth 5
    try {
        $saved = Invoke-RestMethod "$base/api/engagement" -Method Put -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30
    } catch {
        throw "PUT /api/engagement failed: $($_.Exception.Message). Nothing was switched to production."
    }
    $problems = @($saved.guardrails.contract_problems)
    if ($problems.Count -gt 0) { throw "the contract is still incomplete: $($problems -join ', '). Not switching to production." }
    Write-Ok ("contract saved: {0} targets, {1} actions, expires {2}" -f @($Targets).Count, @($Actions).Count, $payload.expires_at)

    # Only now flip the flag: production with no contract refuses every command.
    $stamp = Set-EnvValue -Key 'AV2_ENVIRONMENT' -Value 'production'
    Write-Step "AV2_ENVIRONMENT=production written to .env (backup .env.bak-$stamp)"

    $health = Restart-Av2
    if (-not $health) { throw 'the backend did not come back after the production flip; .env can be restored from the backup printed above.' }
    $engagement = Invoke-RestMethod "$base/api/engagement" -TimeoutSec 30
    $mode = $engagement.guardrails.mode
    $left = @($engagement.guardrails.contract_problems)
    Write-Step ("mode={0} contract_problems={1} targets={2}" -f $mode, $left.Count, (@($engagement.contract.allowed_targets) -join ','))
    if ($mode -ne 'production') { throw "AV2 is in '$mode' mode, not production. Check AV2_ENVIRONMENT in .env." }
    if ($left.Count -gt 0) { throw "production mode with an incomplete contract refuses everything: $($left -join ', ')" }
    Write-Ok 'production guardrails are enforcing the contract'
}

# --------------------------------------------------------------- 9. measure ----
Write-Stage '9. measure the box'
$evalOut = Join-Path $root 'storage\logs\glm53-tools.json'
if (-not $Execute) {
    Write-Step "would run: python tools\glm53\eval_model.py --base-url $baseUrl --suite tools --repeat 2 --out $evalOut"
} elseif ($SkipEval) {
    Write-Step 'skipped (-SkipEval)'
} else {
    $env:SSL_CERT_FILE = $caCert
    & python (Join-Path $here 'eval_model.py') --base-url $baseUrl --api-key $ApiKey --model $Model --suite tools --repeat 2 --out $evalOut
    if ($LASTEXITCODE -ne 0) { Write-Warn "eval_model.py exited $LASTEXITCODE - it is a measurement, not a gate; the box is still serving." }
    else { Write-Ok "scores: $evalOut" }
    Remove-Item Env:\SSL_CERT_FILE -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------ summary ----
Write-Stage 'summary'
Write-Step ("plan        : {0}, {1} GB VRAM, `${2:0.00}/h" -f $spec.Id, $spec.VramGb, $spec.Hourly)
if ($ApproveSpendUsd -gt 0) {
    Write-Step ("approved    : `${0:0.00} = {1} h" -f $ApproveSpendUsd, [math]::Round(($ApproveSpendUsd / $spec.Hourly), 1))
} else {
    Write-Step 'approved    : nothing (plan only)'
}
if ($instanceId -gt 0) { Write-Step ("instance    : {0} (https://{1})" -f $instanceId, $hostName) }
Write-Step ("AV2         : provider=openai, model={0}, fleet=3x GLM" -f $Model)
Write-Step ("environment : {0}" -f $(if ($Production) { 'production (contract enforced)' } else { 'lab' }))
if ($instanceId -gt 0) {
    Write-Host ''
    Write-Host "Stop the meter:  .\tools\glm53\provision_akamai.ps1 -Destroy -InstanceId $instanceId" -ForegroundColor Yellow
}
Write-Host 'Revoke the old Akamai, Render and Hugging Face tokens that were pasted into chat.' -ForegroundColor Yellow




