# Anonymous V2 - one-shot GPU Linode for GLM-5.3-Flash-Uncensored (IQ1_S).
#
#   .\provision_akamai.ps1 -List                  # GPU plans + 730 h cost, no token needed
#   .\provision_akamai.ps1                       # dry run: costs and the exact API calls
#   .\provision_akamai.ps1 -Plan g1-gpu-rtx6000-2 -Execute -BurstHours 8 -OperatorCidr 203.0.113.7/32
#   .\provision_akamai.ps1 -Destroy -InstanceId 1234567
#
# Provisioning is OFF unless -Execute is passed, because the operator asked for no
# resource creation without approval.  The token is read from $env:LINODE_API_TOKEN
# and is never printed, logged or written to disk.
#
# Hardware facts (GET /v4/linode/types, verified 2026-11-22):
#   * Akamai sells the RTX 6000 **Ada** (24 GB) as g1-gpu-rtx6000-{1,2,3,4}.  There is
#     NO "RTX PRO 6000" / Blackwell / 96 GB-per-card plan in the Linode catalog, so
#     "2x RTX PRO 6000" is 2x24 = 48 GB here, not 2x96 = 192 GB.
#   * g1-gpu-rtx6000-2 = 48 GB VRAM + 64 GB RAM at $3.00/h.  71.5 GiB of IQ1_S does
#     not fit in 48 GB, so this script sizes a --n-cpu-moe split instead of pretending
#     it fits; the serve wrapper re-derives the layer count on the box.
#   * g1-gpu-rtx6000-4 = 96 GB VRAM + 128 GB RAM at $6.00/h is the smallest shape
#     that holds the model entirely in VRAM.
#
# Budget gate (hard, by design):
#   * 24/7 mode  : refuse if hourly x 730 + volume > -MonthlyBudgetUsd
#   * burst mode : refuse if hourly x -BurstHours + volume > -MonthlyBudgetUsd
# No GPU plan passes the 24/7 gate: the cheapest is $380/mo and the only shape that
# holds 71.5 GiB entirely in VRAM is $4,380/mo.  Burst mode is how this gets used.
[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter(ParameterSetName = 'List')]
    [switch]$List,

    [Parameter(ParameterSetName = 'Destroy')]
    [switch]$Destroy,

    [Parameter(ParameterSetName = 'Destroy')]
    [int]$InstanceId = 0,

    [string]$Plan = 'g1-gpu-rtx6000-2',
    [string]$Region = 'us-east',
    [string]$Image = 'linode/ubuntu24.04',
    [int]$MonthlyBudgetUsd = 50,
    [double]$BurstHours = 0,
    [string]$OperatorCidr = '',
    [string]$Domain = '',
    [int]$VolumeSizeGb = 0,
    [string]$Label = 'av2-glm53',
    [string]$SshKeyPath = "$HOME\.ssh\id_ed25519.pub",
    [int]$Ctx = 16384,
    [int]$Parallel = 4,
    [string]$OllamaModel = '',
    [int]$WaitMinutes = 0,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$api = 'https://api.linode.com/v4'

# Costs are read by a human, so pin the thread culture: a French Windows renders
# [double] 6 as "6,00", which mixes with the "." thousand separator in the same
# table and makes $1,087.70 look like $1.087,70.  Formatting only - the API calls
# are unaffected.
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

# Per-card VRAM.  The API exposes only gpus/memory/disk for these plans, so the
# per-card figure is keyed off the type id: RTX 4000 Ada is 20 GB, RTX 6000 Ada 24 GB.
$PerCardGb = @{
    rtx4000 = 20
    rtx6000 = 24
}

# Measured from the HF API with ?blobs=true on 2026-11-22:
#   GLM-5.3-Flash-Uncensored-IQ1S  41.89 + 29.63 GiB = 71.52 GiB
#   DeepSeek-V4.1-Flash-1017-IQ1_M 36.66 + 36.49 + 37.25 + 2.04 GiB = 112.44 GiB
$WeightsGb = 71.5
$SecondModelGb = 112.5
$VolumeUsdPerGbMonth = 0.10

# 12 GB of every card is reserved for the 16 k KV cache, CUDA context and
# fragmentation.  An 80 GB plan therefore lands at 68 GB usable and does *not*
# count as "weights resident" for 71.5 GiB - only the 96 GB shape does.
$ReserveGb = 12

function Get-Fit {
    param([double]$NeededGb, [int]$VramGb, [int]$RamGb)
    $usable = [math]::Max(0, $VramGb - $ReserveGb)
    $ram = [math]::Max(0, $RamGb - $ReserveGb)
    if ($usable -ge $NeededGb) { return 'vram' }
    $missing = $NeededGb - $usable
    if ($missing -le $ram) { return ('moe/{0:N1}GB' -f $missing) }
    return 'no'
}

function Get-Token {
    $token = $env:LINODE_API_TOKEN
    if (-not $token) {
        throw "Set LINODE_API_TOKEN first. Mint a NEW token (the one pasted into chat must be revoked) with read/write on Linodes + Volumes only, then: `$env:LINODE_API_TOKEN = '<token>'"
    }
    return $token
}

function Invoke-Linode {
    param(
        [string]$Method = 'GET',
        [string]$Path,
        [object]$Body = $null,
        [switch]$Public
    )
    # Accept-Language is pinned because the API localises its error strings: on a
    # French Windows the plan-limit rejection below arrives as "Demande incorrecte",
    # which cannot be matched or explained.
    $headers = @{ 'Accept' = 'application/json'; 'Accept-Language' = 'en-US' }
    if (-not $Public) { $headers['Authorization'] = "Bearer $(Get-Token)" }
    $params = @{
        Method      = $Method
        Uri         = "$api$Path"
        Headers     = $headers
        TimeoutSec  = 60
        ErrorAction = 'Stop'
    }
    if ($Body) {
        $params['Body'] = ($Body | ConvertTo-Json -Depth 12 -Compress)
        $params['ContentType'] = 'application/json'
    }
    try {
        return Invoke-RestMethod @params
    } catch {
        $detail = $_.Exception.Message
        $reasons = @()
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $detail = "$detail :: $($_.ErrorDetails.Message)"
            # The 400 carries {"errors":[{"reason":"..."}]}; parse it so the plan-limit
            # rejection can be explained instead of dumped as raw JSON.
            try {
                $parsed = $_.ErrorDetails.Message | ConvertFrom-Json
                if ($parsed -and $parsed.errors) { $reasons = @($parsed.errors | ForEach-Object { $_.reason }) }
            } catch { }
        }
        $planId = 'the selected plan'
        if ($Body -is [System.Collections.IDictionary]) {
            # The create body is a hashtable, whose PSObject.Properties are Count/Keys/
            # Values - not its keys - so the plan id has to be read through the indexer.
            # The create body keys the plan as 'type' (Linode's own field name).
            if ($Body.Contains('type')) { $planId = $Body['type'] }
            elseif ($Body.Contains('plan')) { $planId = $Body['plan'] }
        } elseif ($null -ne $Body -and $Body.PSObject.Properties.Name -contains 'plan') {
            $planId = $Body.plan
        }
        foreach ($reason in $reasons) {
            if ("$reason" -match 'limit on your account is preventing the deployment') {
                throw @"
Akamai refuses to deploy $planId on this account:
  "$reason"

This is an account-level restriction, not a credential or script fault: the token
authenticated (GET /account answered 200), the plan exists in the catalog, and the
burst budget passed. Akamai gates GPU plans per account, and an account with PayPal
billing, a `$0.00 balance and no billing history has no GPU plan access.

Verified 2026-11-22: every GPU plan is refused, not just this one - g1-gpu-rtx6000-4
(`$6.00/h), g2-gpu-rtx4000a4-m (`$3.57/h) and g2-gpu-rtx4000a1-s (`$0.52/h) all
return this same 400, so retrying with a smaller card is wasted time.

Nothing was created and nothing was charged. Two ways forward:

  1. Ask Akamai to enable GPU plans. cloud.linode.com > Support > Open Ticket, and
     paste:
       Please enable GPU plan access on this account. Every GPU plan is currently
       rejected with: "A limit on your account is preventing the deployment of the
       selected Linode plan. To request access to the plan, please contact Support
       and provide the Linode plan name." I need "$planId" for a short
       burst deployment of a few hours.
     Approval is manual and is not instant.

  2. Deploy the same 71.5 GiB model on the Azure box that is already scripted:
     one 96 GB RTX PRO 6000 Blackwell at `$1.13/h instead of `$6.00/h.
       .\tools\glm53\azure-deploy.ps1
     See docs/GLM53-AZURE.md.
"@
            }
        }
        throw "Linode API $Method $Path failed: $detail"
    }
}

function Get-GpuPlans {
    $types = (Invoke-Linode -Path '/linode/types' -Public).data
    return $types | Where-Object { $_.gpus -gt 0 } | ForEach-Object {
        $perCard = if ($_.id -match 'rtx4000') { $PerCardGb.rtx4000 } else { $PerCardGb.rtx6000 }
        $card = if ($_.id -match 'rtx4000') { 'RTX 4000 Ada 20G' } else { 'RTX 6000 Ada 24G' }
        $vram = [int]$_.gpus * $perCard
        $ram = [int]($_.memory / 1024)
        $hourly = [double]$_.price.hourly
        [pscustomobject]@{
            Id        = $_.id
            Gpus      = [int]$_.gpus
            Card      = $card
            VramGb    = $vram
            RamGb     = $ram
            Hourly    = $hourly
            Day       = [math]::Round($hourly * 24, 2)
            Month730  = [math]::Round($hourly * 730, 2)
            Glm53     = Get-Fit -NeededGb $WeightsGb -VramGb $vram -RamGb $ram
            DeepSeek  = Get-Fit -NeededGb $SecondModelGb -VramGb $vram -RamGb $ram
        }
    }
}

if ($List) {
    Get-GpuPlans | Sort-Object Hourly |
        Format-Table Id, Gpus, Card, VramGb, RamGb, Hourly, Day, Month730, Glm53, DeepSeek -AutoSize
    Write-Host ""
    Write-Host "Monthly cap: `$$MonthlyBudgetUsd.  Every GPU plan is hourly-billed (monthly is null in the API); 730 h shown for scale." -ForegroundColor DarkGray
    Write-Host "Glm53 / DeepSeek: 'vram' = fully resident, 'moe/xGB' = x GB of experts spill to system RAM," -ForegroundColor DarkGray
    Write-Host "                   'no' = cannot load at all on that shape." -ForegroundColor DarkGray
    Write-Host "GLM IQ1_S = 71.5 GiB, DeepSeek-V4.1 IQ1_M = 112.5 GiB (HF API, ?blobs=true)." -ForegroundColor DarkGray
    return
}

# ---------------------------------------------------------------- teardown -----
if ($Destroy) {
    if ($InstanceId -le 0) { throw '-Destroy needs -InstanceId <id> (the id printed when it was created).' }
    $instance = Invoke-Linode -Path "/linode/instances/$InstanceId"
    Write-Host "deleting instance $($instance.id) [$($instance.label)] $($instance.status)" -ForegroundColor Yellow
    Invoke-Linode -Method DELETE -Path "/linode/instances/$InstanceId" | Out-Null
    $volumes = (Invoke-Linode -Path '/linode/volumes?page_size=500').data |
        Where-Object { $_.label -like "$Label*" -and $_.region -eq $Region }
    foreach ($volume in $volumes) {
        Write-Host "deleting volume $($volume.id) [$($volume.label)] ($($volume.size) GB)" -ForegroundColor Yellow
        Invoke-Linode -Method DELETE -Path "/linode/volumes/$($volume.id)" | Out-Null
    }
    Write-Host 'Deletion requested.  Confirm with -List or the Cloud Manager that nothing is left billing.' -ForegroundColor Green
    return
}

# ------------------------------------------------------------------- budget ----
$spec = Get-GpuPlans | Where-Object { $_.Id -eq $Plan }
if (-not $spec) { throw "Unknown GPU plan '$Plan'.  Run -List to see the ids on this account." }

$volumeMonthly = [math]::Round($VolumeSizeGb * $VolumeUsdPerGbMonth, 2)
if ($BurstHours -gt 0) {
    $mode = "burst $BurstHours h"
    $compute = [math]::Round($spec.Hourly * $BurstHours, 2)
} else {
    $mode = '24/7 (730 h)'
    $compute = [math]::Round($spec.Hourly * 730, 2)
}
$total = [math]::Round($compute + $volumeMonthly, 2)
$affordableHours = if ($spec.Hourly -gt 0) {
    [math]::Floor(($MonthlyBudgetUsd - $volumeMonthly) / $spec.Hourly)
} else { 0 }

[pscustomobject]@{
    Plan            = $spec.Id
    Gpus            = $spec.Gpus
    Card            = $spec.Card
    VramGb          = $spec.VramGb
    RamGb           = $spec.RamGb
    HourlyUsd       = $spec.Hourly
    Billing         = $mode
    ComputeUsd      = $compute
    VolumeUsd       = $volumeMonthly
    TotalUsd        = $total
    BudgetUsd       = $MonthlyBudgetUsd
    BudgetHours     = $affordableHours
    Glm53           = $spec.Glm53
    DeepSeekV41     = $spec.DeepSeek
    OllamaModel     = $(if ($OllamaModel) { $OllamaModel } else { '(none)' })
    WithinBudget    = ($total -le $MonthlyBudgetUsd)
} | Format-List

# ---- feasibility: a plan that cannot hold the weights is not a cheap plan ----
$fatal = @()
if ($spec.Glm53 -eq 'no') {
    $fatal += "$($spec.Id) cannot load GLM IQ1_S at all: $($spec.VramGb) GB VRAM + $($spec.RamGb) GB RAM, minus ${ReserveGb} GB reserve, is less than $WeightsGb GB of weights."
}
if ($OllamaModel -and $spec.DeepSeek -eq 'no') {
    $fatal += "$($spec.Id) cannot load the second model ($SecondModelGb GB) even with CPU offload; it needs a g1-gpu-rtx6000-4 (96 GB VRAM + 128 GB RAM)."
}
if ($OllamaModel -and $spec.Glm53 -ne 'vram') {
    $fatal += "Both models on one box do not fit: GLM IQ1_S alone already spills to system RAM on this shape, and $SecondModelGb GB more cannot follow it."
}

if ($spec.Glm53 -ne 'vram') {
    Write-Host "NOTE: $($spec.Id) has $($spec.VramGb) GB of VRAM; the IQ1_S weights are $WeightsGb GB." -ForegroundColor Yellow
    Write-Host "      The serve wrapper will keep the MoE experts of the excess layers in the $($spec.RamGb) GB of system RAM" -ForegroundColor Yellow
    Write-Host "      (--n-cpu-moe derived from the GGUF block_count).  Expect a few times slower decode than full-VRAM." -ForegroundColor Yellow
    Write-Host "      Full-VRAM shapes on Akamai: g1-gpu-rtx6000-4 (96 GB, 6.00/h) or g2-gpu-rtx4000a4-m (80 GB, 3.57/h)." -ForegroundColor Yellow
}
foreach ($reason in $fatal) { Write-Host "REFUSED: $reason" -ForegroundColor Red }
if ($fatal.Count -gt 0) {
    if ($Execute) { throw 'Feasibility gate refused this request; nothing was created.' }
    Write-Host 'Dry run only - nothing was created.' -ForegroundColor DarkGray
    return
}

if ($total -gt $MonthlyBudgetUsd) {
    $cheapest = (Get-GpuPlans | Sort-Object Hourly | Select-Object -First 1)
    Write-Host ""
    Write-Host (("REFUSED: {0} costs `${1} as {2}, over the `${3} cap." -f $spec.Id, $total, $mode, $MonthlyBudgetUsd)) -ForegroundColor Red
    Write-Host (("          As a burst that budget buys about {0} h on {1} (`${2}/h)." -f $affordableHours, $spec.Id, $spec.Hourly)) -ForegroundColor Yellow
    Write-Host (("          Cheapest GPU plan on the account: {0} at `${1}/mo." -f $cheapest.Id, $cheapest.Month730)) -ForegroundColor Yellow
    Write-Host (("          Azure credit cannot pay for this: Akamai bills the card on the account.")) -ForegroundColor Yellow
    if ($Execute) { throw 'Budget gate refused this request; nothing was created.' }
    Write-Host '          Dry run only - pass -BurstHours <n> to fit inside the cap, then -Execute.' -ForegroundColor DarkGray
    return
}

# ---------------------------------------------------------------- dry run -----
if (-not $Execute) {
    Write-Host ""
    Write-Host "DRY RUN - nothing created.  With -Execute this would:" -ForegroundColor Cyan
    Write-Host "  1. POST /linode/instances          {type=$Plan, image=$Image, region=$Region, metadata.user_data=<cloud-init>}"
    Write-Host "     cloud-init from tools/glm53/cloud-init.yaml with:"
    Write-Host "       API_KEY            = <random 32-byte bearer token, generated here>"
    Write-Host "       OPERATOR_CIDR      = $($(if ($OperatorCidr) { $OperatorCidr } else { '<set -OperatorCidr>' }))"
    Write-Host "       CADDY_SITE         = $($(if ($Domain) { $Domain } else { 'https://<instance-ip>' }))"
    Write-Host "       CADDY_INTERNAL_TLS = $($(if ($Domain) { '0' } else { '1' }))"
    Write-Host "       PLAN_VRAM_GB/RAM_GB= $($spec.VramGb) / $($spec.RamGb)   (from the Linode type object)"
    Write-Host "       WEIGHTS_GB         = $WeightsGb"
    Write-Host "       CTX / PARALLEL     = $Ctx / $Parallel"
    Write-Host "       OLLAMA_MODEL       = $($(if ($OllamaModel) { $OllamaModel } else { '(empty - Ollama not installed)' }))"
    Write-Host "  2. bootstrap oneshot: fetch 71.5 GiB -> CUDA build -> Caddy -> llama-server -> /etc/av2-glm53/ready"
    Write-Host "  3. the serve wrapper decides the GPU split from the GGUF block_count:"
    if ($spec.Glm53 -eq 'vram') {
        Write-Host "     $($spec.VramGb) GB of VRAM holds the $WeightsGb GB of weights, so it serves -ngl 999 with no CPU experts"
    } else {
        Write-Host "     $($spec.VramGb) GB of VRAM is used as far as it goes and the rest of the experts stay in system RAM"
        Write-Host "     (--n-cpu-moe is derived, not guessed)"
    }
    if ($WaitMinutes -gt 0) {
        Write-Host "  4. -WaitMinutes $WaitMinutes : poll https://<ip>/v1/models until the model answers"
    }
    Write-Host ""
    Write-Host "Next: provision with -Execute, or skip Akamai entirely (see docs/GLM53-SELFHOST.md s.4)." -ForegroundColor DarkGray
    return
}

# -------------------------------------------------------------- provision ------
# Volume handling is deliberately left out: attaching a block volume after first
# boot races the bootstrap oneshot, and the weights are only needed for one burst.
if ($VolumeSizeGb -ne 0) {
    throw 'Leave -VolumeSizeGb 0. Attaching a volume after boot races the bootstrap; a burst run keeps the 71.5 GiB on the instance disk and re-fetches it if rebuilt.'
}
if ($OperatorCidr -notmatch '^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$') {
    throw '-OperatorCidr must be a single IPv4 CIDR, e.g. 203.0.113.7/32. Refusing to expose 22/443 to the world.'
}
if (-not (Test-Path -LiteralPath $SshKeyPath)) { throw "SSH public key not found: $SshKeyPath (override with -SshKeyPath)." }
$sshKey = (Get-Content -LiteralPath $SshKeyPath -Raw).Trim()
if ($sshKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-short-)') { throw "$SshKeyPath does not look like an OpenSSH public key." }

$templatePath = Join-Path $PSScriptRoot 'cloud-init.yaml'
if (-not (Test-Path -LiteralPath $templatePath)) { throw "cloud-init template missing: $templatePath" }
$template = Get-Content -LiteralPath $templatePath -Raw

function New-RandomString {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# The box serves Caddy's internal CA until a real domain is attached, so the probe
# passes -k; the bearer token is still required, so "answers" cannot be spoofed by
# something else on 443.  curl.exe ships with Windows 10+ and returns a trustworthy
# status code, which the .NET HttpClient path did not on this machine (TLS send
# failure in PowerShell 5.1, verified 2026-11-22).  The token is never printed.
function Wait-ModelEndpoint {
    param(
        [Parameter(Mandatory)][string]$DnsName,
        [Parameter(Mandatory)][string]$Token,
        [int]$TimeoutMinutes = 45
    )
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    if (-not $curl) {
        throw 'curl.exe not found; the readiness probe needs it (Windows 10+ ships it). Use -WaitMinutes 0 and poll from the box instead.'
    }
    $url = "https://$DnsName/v1/models"
    $body = Join-Path $env:TEMP ('av2-models-{0}.json' -f [guid]::NewGuid().ToString('N'))
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $attempt = 0
    while ((Get-Date) -lt $deadline) {
        $attempt++
        $code = & $curl -k -s -o $body -w '%{http_code}' -m 20 -H "Authorization: Bearer $Token" $url 2>$null
        if ($code -eq '200') {
            $json = Get-Content -LiteralPath $body -Raw
            Remove-Item -LiteralPath $body -ErrorAction SilentlyContinue
            return $json
        }
        if ($code -eq '401' -or $code -eq '403') {
            # Serving TLS but rejecting the token would mean the wrong instance answered.
            Remove-Item -LiteralPath $body -ErrorAction SilentlyContinue
            throw "$url answered HTTP $code - something is listening but it rejects the generated token."
        }
        Write-Host ("  probe {0}: HTTP {1}" -f $attempt, $(if ($code) { $code } else { 'no response' })) -ForegroundColor DarkGray
        Start-Sleep -Seconds 20
    }
    Remove-Item -LiteralPath $body -ErrorAction SilentlyContinue
    return $null
}

$apiKey = 'av2_' + (New-RandomString 32)
$rootPass = (New-RandomString 21) + 'Az9!'
$modelDir = '/opt/av2-models'
$caddySite = if ($Domain) { $Domain } else { 'auto' }
$caddyInternal = if ($Domain) { '0' } else { '1' }

$userData = $template.
    Replace('__API_KEY__', $apiKey).
    Replace('__OPERATOR_CIDR__', $OperatorCidr).
    Replace('__CADDY_SITE__', $caddySite).
    Replace('__CADDY_INTERNAL_TLS__', $caddyInternal).
    Replace('__MODEL_DIR__', $modelDir).
    Replace('__PLAN_VRAM_GB__', [string]$spec.VramGb).
    Replace('__PLAN_RAM_GB__', [string]$spec.RamGb).
    Replace('__WEIGHTS_GB__', [string]$WeightsGb).
    Replace('__CTX__', [string]$Ctx).
    Replace('__PARALLEL__', [string]$Parallel).
    Replace('__OLLAMA_MODEL__', $OllamaModel)
if ($userData -match '__[A-Z_]+__') {
    $left = [regex]::Matches($userData, '__[A-Z_]+__') | ForEach-Object { $_.Value } | Select-Object -Unique
    throw "cloud-init placeholders left unfilled: $($left -join ', ')"
}

$regionCheck = $null
try {
    $regionCheck = Invoke-Linode -Path "/regions/$Region/availability"
} catch {
    Write-Host "note: could not read region availability for ${Region}: $($_.Exception.Message)" -ForegroundColor DarkGray
}
$planAvailable = $true
if ($regionCheck -and $regionCheck.available) {
    $available = $regionCheck.available
    if ($available.PSObject.Properties.Name -contains $Plan) { $planAvailable = [bool]$available.$Plan }
}
if (-not $planAvailable) { throw "$Plan is not available in $Region right now. Try another -Region." }

$body = @{
    image           = $Image
    region          = $Region
    type            = $Plan
    label           = $Label
    root_pass       = $rootPass
    authorized_keys = @($sshKey)
    tags            = @('av2', 'glm53', ('burst-' + $BurstHours + 'h'))
    booted          = $true
    metadata        = @{ user_data = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($userData)) }
}
Write-Host "creating $Plan in $Region ..." -ForegroundColor Cyan
$instance = Invoke-Linode -Method POST -Path '/linode/instances' -Body $body

$ip = ($instance.ipv4 | Select-Object -First 1)
$host_ = if ($Domain) { $Domain } else { $ip }
$caHint = if ($Domain) {
    "public certificate (no SSL_CERT_FILE needed)"
} else {
    "internal CA - copy /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt from the box and point SSL_CERT_FILE at it"
}

Write-Host ""
Write-Host "instance $($instance.id) created: https://$host_" -ForegroundColor Green
Write-Host "  plan      : $($spec.Id) - $($spec.Gpus)x $($spec.Card), $($spec.VramGb) GB VRAM + $($spec.RamGb) GB RAM, `$$($spec.Hourly)/h"
Write-Host "  model fit : $($spec.Glm53)  (vram = resident on the cards, moe = experts spill to RAM)"
Write-Host "  bootstrap : ssh root@$ip 'journalctl -u av2-glm53-bootstrap -f'   (weights + CUDA build: 20-40 min)"
Write-Host "  ready when: /etc/av2-glm53/ready exists, i.e. GET /v1/models answered with the token"
Write-Host "  TLS       : $caHint"
Write-Host "  token     : stored only in /etc/av2-glm53/env on the box; run tools/glm53/connect_av2.ps1 to wire AV2"
Write-Host "  cost      : `$$($spec.Hourly)/h - delete with: .\provision_akamai.ps1 -Destroy -InstanceId $($instance.id)"
if ($OllamaModel) {
    Write-Host "  ollama    : pulling $OllamaModel in the background - ssh root@$ip 'tail -f /var/log/av2-glm53-ollama-pull.log'"
    Write-Host "              it is loopback-only on 11434; AV2 talks to one endpoint, so this is for direct Ollama use"
}
Write-Host ""
Write-Host "The API key is NOT echoed here on purpose. Read it from the box when you wire AV2:" -ForegroundColor DarkGray
Write-Host "  ssh root@$ip 'grep ^API_KEY /etc/av2-glm53/env'" -ForegroundColor DarkGray

if ($WaitMinutes -gt 0) {
    Write-Host ""
    Write-Host "waiting up to $WaitMinutes min for https://$host_/v1/models ..." -ForegroundColor Cyan
    $models = Wait-ModelEndpoint -DnsName $host_ -Token $apiKey -TimeoutMinutes $WaitMinutes
    if ($models) {
        Write-Host "model endpoint answered:" -ForegroundColor Green
        Write-Host $models
    } else {
        Write-Host "endpoint did not answer within $WaitMinutes min.  The instance is still billing at \`$$($spec.Hourly)/h." -ForegroundColor Red
        Write-Host "  diagnose: ssh root@$ip 'journalctl -u av2-glm53-bootstrap -n 80; journalctl -u llama-server -n 80'" -ForegroundColor Yellow
        Write-Host "  or stop the spend: .\provision_akamai.ps1 -Destroy -InstanceId $($instance.id)" -ForegroundColor Yellow
        exit 1
    }
}


