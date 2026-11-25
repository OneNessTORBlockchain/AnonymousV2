# Anonymous V2 - provision the self-hosted GLM-5.3-Flash-Uncensored GPU VM on Azure.
#
#   # 1. look before you spend (no writes, no az mutations)
#   .\azure-deploy.ps1 -OperatorCidr auto
#
#   # 2. create it
#   .\azure-deploy.ps1 -OperatorCidr auto -Execute
#
#   # 3. wire AV2 to the local relay (see docs/GLM53-AZURE.md)
#   python .\wake_proxy.py --upstream https://<ip> --upstream-key <key> --relay-key <key>
#   .\connect_av2.ps1 -BaseUrl http://127.0.0.1:8430/v1 -ApiKey <relay-key> -Apply
#
# Dry run is the default on purpose.  -Execute is required before anything in
# Azure is created, and the only mutations are inside -ResourceGroup: one NSG,
# one static public IP, one NIC, one VM (plus optional auto-shutdown schedule).
# Nothing existing is modified.
#
# Cost shape (verified against the public Azure retail price API, not guessed),
# measured 2026-11-22 in eastus, Linux, pay-as-you-go:
#   Standard_NC24lds_xl_RTXPRO6000BSE_v6  1x RTX PRO 6000 Blackwell SE 96 GB
#     24 vCPU / 72 GiB RAM / 1.6 TiB local NVMe        $1.130/h  <- default here
#     71.52 GiB of IQ1_S weights fit in VRAM whole, so no CPU offload, no disk I/O
#     per token.  $200 buys ~177 GPU-hours.
#   Standard_NC36ds_xl_RTXPRO6000BSE_v6   36 vCPU / 132 GiB RAM, same 96 GB card
#   Standard_NC24ads_A100_v4              1x A100 80 GB  $3.673/h (~54 GPU-hours)
#   Standard_NC64as_T4_v3                 4x T4 16 GB    $4.352/h
#   Standard_NC16as_T4_v3                 1x T4 16 GB, 110 GiB RAM  $1.204/h
#                                          16 GB cannot hold 71.52 GiB: it only pays
#                                          off as RAM-offload assist, never as the
#                                          primary device.
# Spot is deliberately absent: -UseSpot exists but the production posture for this
# box is pay-as-you-go, and eviction costs a full cold start.
# A deallocated VM still bills its managed disk (see the printed table), so the
# credit does not stop draining while the box is off; it only stops draining fast.
[CmdletBinding()]
param(
    [string]$SubscriptionId = '63a43e4c-9301-438b-9efe-3ca8666c6198',
    [string]$ResourceGroup = 'anon',
    [string]$Region = 'eastus',
    [string]$VmName = 'av2-glm53',
    [string]$VmSize = 'Standard_NC24lds_xl_RTXPRO6000BSE_v6',
    [int]$OsDiskGiB = 128,
    [ValidateSet('Premium_LRS', 'StandardSSD_LRS')]
    [string]$DiskSku = 'Premium_LRS',
    [switch]$UseSpot,
    [string]$OperatorCidr = 'auto',
    [string]$SshPublicKeyPath = '',
    [string]$SshPublicKey = '',
    [string]$ApiKey = '',
    [string]$ModelDir = '/opt/models/glm53',
    # 0 = keep the GPU running.  Production wants a warm model: a deallocated VM
    # needs 8-12 min to re-read 71.5 GiB before it can answer, which is exactly the
    # latency this deployment exists to avoid.  The stop is explicit (teardown, or
    # the relay's spend cap), not a timer.
    [int]$IdleMinutes = 0,
    [string]$AutoShutdownTime = '',
    [double]$BudgetUsd = 200,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --------------------------------------------------------------------------- plan
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$cloudInitSource = Join-Path $PSScriptRoot 'azure-cloud-init.yaml'
$renderedPath = Join-Path ([System.IO.Path]::GetTempPath()) "av2-glm53-$stamp.cloud-init.yaml"
$nsgName = "$VmName-nsg"
$ipName = "$VmName-ip"
$nicName = "$VmName-nic"

function Write-Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "   $Text" -ForegroundColor DarkGray }
function Write-Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }
function Write-Fail([string]$Text) { Write-Host "   $Text" -ForegroundColor Red }

# --- GPU profile table ---------------------------------------------------------
# Two numbers llama.cpp needs that must be decided before the box boots:
#   Arch    - the CUDA compute capability to compile for.  A Blackwell card
#             (sm_120) needs a toolkit >= 12.8; compiling sm_80 for it produces a
#             binary that silently never touches the GPU.
#   Ngl     - layers offloaded to VRAM.  999 only when the whole 71.52 GiB of
#             IQ1_S weights fits in VRAM; offloading more than fits fails at load
#             time with an OOM the service then retries forever.
$gpuProfiles = @{
    'Standard_NC24lds_xl_RTXPRO6000BSE_v6' = @{ Vram = 96; Arch = 120; Toolkit = '12-8'; Ngl = 999; Card = 'RTX PRO 6000 Blackwell Server Edition' }
    'Standard_NC36ds_xl_RTXPRO6000BSE_v6'  = @{ Vram = 96; Arch = 120; Toolkit = '12-8'; Ngl = 999; Card = 'RTX PRO 6000 Blackwell Server Edition' }
    'Standard_NC72ds_xl_RTXPRO6000BSE_v6'  = @{ Vram = 96; Arch = 120; Toolkit = '12-8'; Ngl = 999; Card = 'RTX PRO 6000 Blackwell Server Edition' }
    'Standard_NC24ads_A100_v4'             = @{ Vram = 80; Arch = 80;  Toolkit = '12-4'; Ngl = 999; Card = 'NVIDIA A100 80 GB' }
    'Standard_NC48ads_A100_v4'             = @{ Vram = 80; Arch = 80;  Toolkit = '12-4'; Ngl = 999; Card = 'NVIDIA A100 80 GB' }
    'Standard_NC96ads_A100_v4'             = @{ Vram = 80; Arch = 80;  Toolkit = '12-4'; Ngl = 999; Card = 'NVIDIA A100 80 GB' }
    'Standard_NC4as_T4_v3'                 = @{ Vram = 16; Arch = 75;  Toolkit = '12-4'; Ngl = 0;   Card = 'Tesla T4 16 GB' }
    'Standard_NC8as_T4_v3'                 = @{ Vram = 16; Arch = 75;  Toolkit = '12-4'; Ngl = 0;   Card = 'Tesla T4 16 GB' }
    'Standard_NC16as_T4_v3'                = @{ Vram = 16; Arch = 75;  Toolkit = '12-4'; Ngl = 0;   Card = 'Tesla T4 16 GB' }
    'Standard_NC64as_T4_v3'                = @{ Vram = 64; Arch = 75;  Toolkit = '12-4'; Ngl = 0;   Card = '4x Tesla T4 16 GB' }
}
$gpu = $gpuProfiles[$VmSize]
if (-not $gpu) {
    $gpu = @{ Vram = 0; Arch = 80; Toolkit = '12-4'; Ngl = 0; Card = 'unrecognised' }
    Write-Warn "no GPU profile for ${VmSize}: assuming sm_80 and no layer offload"
}
if ($gpu.Vram -gt 0 -and $gpu.Vram -lt 72) {
    Write-Warn ("{0} provides {1} GB of VRAM against 71.52 GiB of weights: it cannot hold the model, only assist it." -f $gpu.Card, $gpu.Vram)
}

function Get-AzJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $raw = & az @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("az {0} failed: {1}" -f ($Arguments -join ' '), ($raw | Out-String).Trim()) }
    if (-not $raw) { return $null }
    return ($raw | Out-String | ConvertFrom-Json)
}

function Get-RetailPrice {
    <#
      Reads the public Azure retail price API (no authentication needed) so the
      numbers in the plan are the ones Microsoft publishes, not recollection.
    #>
    param([string]$Sku, [string]$RegionName, [bool]$Spot)

    $filter = "armSkuName eq '$Sku' and armRegionName eq '$RegionName' and priceType eq 'Consumption'"
    $uri = 'https://prices.azure.com/api/retail/prices?$filter=' + [uri]::EscapeDataString($filter)
    try {
        $payload = Invoke-RestMethod -Uri $uri -TimeoutSec 45
    } catch {
        Write-Warn "retail price API unreachable: $($_.Exception.Message)"
        return $null
    }
    $items = @($payload.Items | Where-Object { $_.productName -match 'Linux' -and $_.meterName -notmatch 'Low Priority' })
    $pick = if ($Spot) {
        $items | Where-Object { $_.skuName -match 'Spot' -or $_.meterName -match 'Spot' } | Select-Object -First 1
    } else {
        $items | Where-Object { $_.skuName -notmatch 'Spot|Low Priority' -and $_.type -eq 'Consumption' } | Select-Object -First 1
    }
    if (-not $pick) { return $null }
    return [pscustomobject]@{
        HourlyUsd = [double]$pick.retailPrice
        Meter     = $pick.meterName
        SkuName   = $pick.skuName
        Region    = $pick.armRegionName
    }
}

function Get-DiskUsdPerMonth([int]$Gib, [string]$Sku) {
    # Published managed-disk rates per GB-month, Linux, pay-as-you-go.
    $rate = if ($Sku -eq 'Premium_LRS') { 0.154 } else { 0.075 }
    return [math]::Round($Gib * $rate, 2)
}

Write-Host 'AV2 self-hosted GLM-5.3 on Azure' -ForegroundColor Green
Write-Host ("  model      : BoldingBuilds/GLM-5.3-Flash-Uncensored-GGUF (IQ1_S, 71.5 GiB, text-only)")
Write-Host ("  resource   : {0} / {1} / {2}" -f $ResourceGroup, $Region, $VmName)
Write-Host ("  size       : {0}{1}" -f $VmSize, $(if ($UseSpot) { ' (Spot)' } else { ' (pay-as-you-go)' }))
Write-Host ("  gpu        : {0}, {1} GB VRAM, sm_{2}, -ngl {3}" -f $gpu.Card, $gpu.Vram, $gpu.Arch, $gpu.Ngl)
Write-Host ("  mode       : {0}" -f $(if ($Execute) { 'EXECUTE - this creates billable Azure resources' } else { 'dry run' }))

# --------------------------------------------------------------------------- preflight
Write-Step 'Preflight (read-only)'

function Resolve-AzCli {
    <#
      PATH is not reliable here: a freshly installed CLI is only visible to shells
      started after the install, and the Azure CLI has three common homes on Windows.
      Resolve it explicitly, then prepend its directory so every `az` call below works.
    #>
    $onPath = Get-Command az -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $candidates = @(
        'C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd',
        'C:\Program Files (x86)\Microsoft SDKs\Azure\CLI2\wbin\az.cmd',
        (Join-Path $env:APPDATA 'Python\Python311\Scripts\az.bat'),
        (Join-Path $env:APPDATA 'Python\Python312\Scripts\az.bat'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\Scripts\az.bat')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    foreach ($root in @((Join-Path $env:APPDATA 'Python'), (Join-Path $env:LOCALAPPDATA 'Programs\Python'))) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $hit = Get-ChildItem -LiteralPath $root -Filter 'az.bat' -Recurse -Depth 3 -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

$azCli = Resolve-AzCli
if (-not $azCli) {
    Write-Fail 'az CLI not found on PATH or in the usual install locations.'
    Write-Note 'Install it first:  winget install --id Microsoft.AzureCLI --accept-source-agreements --accept-package-agreements'
    Write-Note 'or without admin:  python -m pip install --user azure-cli'
    Write-Note 'Then authenticate:  az login --use-device-code'
    Write-Note 'Nothing was created.  The identifiers pasted into chat (subscription/tenant/client) cannot authenticate on their own:'
    Write-Note 'az needs a signed-in user, a client secret, or a federated credential.  No secret was supplied and none is needed here.'
    exit 2
}
$azDir = Split-Path -Parent $azCli
if ($azDir -and $env:Path -notlike "*$azDir*") { $env:Path = "$azDir;$env:Path" }
Write-Note "az: $azCli"

try {
    $account = Get-AzJson @('account', 'show', '-o', 'json')
} catch {
    Write-Fail 'not signed in to Azure (az account show failed).'
    Write-Note 'Run:  az login --use-device-code'
    Write-Note 'then: az account set --subscription ' + $SubscriptionId
    exit 2
}
Write-Note ("signed in as {0} on tenant {1}" -f $account.user.name, $account.tenantId)
if ($account.id -ne $SubscriptionId) {
    Write-Warn ("current subscription is {0}, not {1}; the plan below is for the current one" -f $account.id, $SubscriptionId)
    Write-Note 'switch with:  az account set --subscription ' + $SubscriptionId
}

# --- offer gate: the reason a brand-new subscription cannot run this at all ----
# Quota is not the first gate.  The subscription's *offer* decides which VM
# families exist for it.  Free Trial / MSDN / student offers exclude the NC, ND
# and NV families outright: their quota reads 0 and every raise is refused with
# ResourceNotAvailableForOffer, so no amount of retrying or region-hopping helps.
# Measured on this subscription 2026-11-22 (see docs/GLM53-AZURE.md 3.2).
$offerId = ''
$spendLimit = ''
try {
    $sub = Get-AzJson @('rest', '--method', 'get',
                        '--uri', "/subscriptions/$SubscriptionId`?api-version=2022-12-01", '-o', 'json')
    $offerId = [string]$sub.subscriptionPolicies.quotaId
    $spendLimit = [string]$sub.subscriptionPolicies.spendingLimit
} catch {
    Write-Warn "could not read subscription policies: $($_.Exception.Message)"
}
if ($offerId) {
    Write-Note ("subscription offer: {0} (spending limit {1})" -f $offerId, $(if ($spendLimit) { $spendLimit } else { 'unknown' }))
}
if ($offerId -match '^(FreeTrial|MSDN|BizSpark|DreamSpark|AzureForStudents|AzurePass)') {
    Write-Fail ("offer '{0}' does not include the {1} family: provisioning is impossible, not merely unapproved." -f $offerId, $VmSize)
    Write-Note 'Evidence gathered on this subscription (all of it reproducible, none of it guessed):'
    Write-Note '  az vm list-skus -l eastus -> every NC/ND/NV SKU: restriction NotAvailableForSubscription'
    Write-Note '                               or quota 0 in its family (Standard NCASv3_T4 Family = 0/0).'
    Write-Note '  az quota update ... -> ResourceNotAvailableForOffer for A100 v4, NCASv3_T4, RTX PRO 6000'
    Write-Note '                         BSE v6, and even standardESv5Family (a plain CPU family).'
    Write-Note '  az vm create --validate --size Standard_NC4as_T4_v3 -> QuotaExceeded,'
    Write-Note '                         "Current Limit: 0 ... (Minimum) New Limit Required: 4".'
    Write-Note '  every CPU family -> limit 4 vCPUs, so the largest creatable VM is 4 vCPU / 32 GiB,'
    Write-Note '                      against 71.52 GiB of weights.  There is no VM of any kind that fits.'
    Write-Note 'Fix (operator action, roughly two minutes, cannot be done by a script):'
    Write-Note '  Portal -> Subscriptions -> this subscription -> Upgrade to pay-as-you-go.'
    Write-Note '  That clears the spending limit and makes GPU quota requestable.  Then either'
    Write-Note '  raise quota (Portal -> Quotas -> Compute -> the family named above) or open'
    Write-Note '  Help + support -> Service and subscription limits (quotas).'
    Write-Note 'Nothing was created.  Re-run this script unchanged once the offer is upgraded.'
    exit 3
}

$skuInfo = $null
try {
    $skus = Get-AzJson @('vm', 'list-skus', '-l', $Region, '--resource-type', 'virtualMachines',
                         '--size', $VmSize, '-o', 'json')
    $skuInfo = $skus | Where-Object { $_.name -eq $VmSize } | Select-Object -First 1
} catch {
    Write-Warn "could not list SKUs: $($_.Exception.Message)"
}

# --- quota gate: the family is read from the SKU, never assumed -----------------
# The A100 series, the T4 series and the RTX PRO 6000 series live in three
# different quota families, and a family name that does not match the SKU reports
# a healthy limit for a VM that cannot be built.  Ask the SKU what family it is in.
$quotaFamily = if ($skuInfo -and $skuInfo.family) { [string]$skuInfo.family } else { $null }
$quota = $null
try {
    $usage = Get-AzJson @('vm', 'list-usage', '-l', $Region, '-o', 'json')
    if ($quotaFamily) {
        $quota = $usage | Where-Object { $_.name.value -eq $quotaFamily } | Select-Object -First 1
    }
    if (-not $quota) { $quota = $usage | Where-Object { $_.name.value -match 'NCA' -or $_.name.value -match 'NCADS' } | Select-Object -First 1 }
} catch {
    Write-Warn "could not read quota: $($_.Exception.Message)"
}
$quotaLine = 'unknown'
if ($quota) { $quotaLine = '{0} of {1} vCPUs' -f $quota.currentValue, $quota.limit }
Write-Note ("quota family {0} in {1}: {2}" -f $(if ($quota) { $quota.name.value } else { $quotaFamily }), $Region, $quotaLine)
if ($quota -and [int]$quota.limit -le 0) {
    Write-Warn 'quota limit is 0: the VM create WILL be rejected until this family is enabled for the subscription.'
    Write-Note 'A zero limit on a GPU family is not a capacity blip; it is the offer refusing the family.'
    Write-Note 'The self-serve raise (az quota update) answers ResourceNotAvailableForOffer on such an offer.'
}
if ($skuInfo) {
    $zones = (@($skuInfo.locationInfo | ForEach-Object { $_.zones }) | Where-Object { $_ }) -join ','
    $restricted = [bool](@($skuInfo.restrictions) | Where-Object { $_.type -eq 'Location' })
    # A Zone restriction carrying NotAvailableForSubscription is the one that really
    # blocks a new subscription: it is subscription eligibility, not a capacity blip.
    $notForSub = @($skuInfo.restrictions | Where-Object { $_.reasonCode -eq 'NotAvailableForSubscription' })
    Write-Note ("SKU {0} in {1}: zones {2}{3}" -f $VmSize, $Region, $(if ($zones) { $zones } else { 'none' }), $(if ($restricted) { ' (RESTRICTED for this subscription)' } else { '' }))
} else {
    Write-Warn "SKU $VmSize not offered in $Region"
}
if ($notForSub) {
    Write-Fail ("{0} is listed in {1} but NOT available to this subscription (NotAvailableForSubscription)." -f $VmSize, $Region)
    Write-Note 'az vm create fails with SkuNotAvailable for this; it is an eligibility block, not capacity.'
}

# --- price + budget math ------------------------------------------------------
# Numbers that end up inside a printed `az`/`python` command must use invariant
# formatting: on a French-locale host 3.673 renders as "3,673" and the copied
# command line then means something else entirely.
$inv = [System.Globalization.CultureInfo]::InvariantCulture

$price = Get-RetailPrice -Sku $VmSize -RegionName $Region -Spot:$UseSpot
$diskUsd = Get-DiskUsdPerMonth -Gib $OsDiskGiB -Sku $DiskSku
if ($price) {
    $hourlyText = ([double]$price.HourlyUsd).ToString('0.000', $inv)
    Write-Note ("price: {0} at {1} USD/h ({2})" -f $price.SkuName, $hourlyText, $price.Meter)
} else {
    # Verified 2026-11-22 against the retail price API, Linux, eastus, PAYG.
    # Used only when that API is unreachable - never as a silent substitute.
    $verifiedPayg = @{
        'Standard_NC24lds_xl_RTXPRO6000BSE_v6' = 1.130
        'Standard_NC24ads_A100_v4'             = 3.673
    }
    $fallback = $verifiedPayg[$VmSize]
    if ($fallback) {
        $price = [pscustomobject]@{ HourlyUsd = $fallback; Meter = 'assumed (verified 2026-11-22)'; SkuName = $VmSize }
        Write-Warn ("live price unavailable; using the verified {0} figure {1} USD/h" -f $Region, ([double]$fallback).ToString('0.000', $inv))
    } else {
        $price = [pscustomobject]@{ HourlyUsd = 0; Meter = 'unknown'; SkuName = $VmSize }
        Write-Warn 'live price unavailable and no verified figure for this SKU; the budget arithmetic is skipped'
    }
    $hourlyText = ([double]$price.HourlyUsd).ToString('0.000', $inv)
}

# --- operator network ---------------------------------------------------------
if ($OperatorCidr -eq 'auto') {
    try {
        $myIp = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 20).ip
        $OperatorCidr = "$myIp/32"
        Write-Note "operator CIDR resolved from this machine: $OperatorCidr"
        Write-Warn 'a residential IP changes; the NSG rule has to be updated when it does.'
    } catch {
        Write-Fail 'could not resolve this machine public IP; pass -OperatorCidr <a.b.c.d/32>'
        exit 2
    }
}
if ($OperatorCidr -notmatch '^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$') {
    Write-Fail "-OperatorCidr '$OperatorCidr' is not a CIDR. Refusing to open SSH/HTTPS to the world."
    exit 2
}
Write-Note "operator CIDR: $OperatorCidr (only source allowed to reach 22 and 443)"

# --- ssh key ------------------------------------------------------------------
if (-not $SshPublicKey) {
    $candidate = if ($SshPublicKeyPath) { $SshPublicKeyPath } else { Join-Path $HOME '.ssh\id_ed25519.pub' }
    if (Test-Path -LiteralPath $candidate) {
        $SshPublicKey = (Get-Content -LiteralPath $candidate -Raw).Trim()
        $SshPublicKeyPath = $candidate
    } else {
        Write-Fail "no SSH public key: pass -SshPublicKey or -SshPublicKeyPath (looked at $candidate)"
        exit 2
    }
}
if ($SshPublicKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-)') {
    Write-Fail 'that does not look like an SSH public key (a private key must never be pasted here).'
    exit 2
}
$keyHint = $SshPublicKey.Substring(0, [Math]::Min(24, $SshPublicKey.Length)) + '...'
Write-Note ("ssh key: {0}{1}" -f $keyHint, $(if ($SshPublicKeyPath) { " ($SshPublicKeyPath)" } else { '' }))

# --- api key ------------------------------------------------------------------
if (-not $ApiKey) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $ApiKey = 'av2_' + ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '').Substring(0, 32)
    Write-Note 'generated a llama-server API key for this deployment'
}
$maskedKey = if ($ApiKey.Length -gt 10) { $ApiKey.Substring(0, 6) + '...' + $ApiKey.Substring($ApiKey.Length - 4) } else { '<short>' }
Write-Note "llama-server api key: $maskedKey"

# --- budget table -------------------------------------------------------------
$hoursText = if ($price.HourlyUsd -gt 0) { ([double]($BudgetUsd / $price.HourlyUsd)).ToString('0.0', $inv) } else { '0' }
$idlePerDayText = ([double]($diskUsd / 30)).ToString('0.00', $inv)
$diskText = ([double]$diskUsd).ToString('0.00', $inv)
$sessionText = ([double]($price.HourlyUsd * (0.5 + $IdleMinutes / 60))).ToString('0.00', $inv)
$budgetText = ([double]$BudgetUsd).ToString('0', $inv)
# Continuous-run numbers the operator asked for explicitly: 24 h and 30 days.
$dayText = ([double]($price.HourlyUsd * 24)).ToString('0.00', $inv)
$monthText = ([double]($price.HourlyUsd * 24 * 30)).ToString('0.00', $inv)
$warnUsd = [math]::Round($BudgetUsd * 0.75, 2)
$warnText = ([double]$warnUsd).ToString('0.00', $inv)

Write-Step 'Cost picture'
Write-Host ("  GPU        : {0} USD/h  ->  {1} USD buys {2} GPU-hours" -f $hourlyText, $budgetText, $hoursText)
Write-Host ("  continuous : {0} USD per 24 h, {1} USD per 30 days of GPU time" -f $dayText, $monthText) -ForegroundColor Yellow
Write-Host ("  disk idle  : {0} GiB {1} = {2} USD/month ({3} USD/day) - billed while DEALLOCATED" -f $OsDiskGiB, $DiskSku, $diskText, $idlePerDayText)
if ($IdleMinutes -gt 0) {
    Write-Host ("  session    : 30 min warm, then {0} min idle -> ~{1} USD of GPU" -f $IdleMinutes, $sessionText)
} else {
    Write-Host  '  session    : always-on - GPU bills until the spend cap or teardown stops it' -ForegroundColor Yellow
}
Write-Host  '  excluded   : one-time bootstrap (~10 min of GPU while apt/NVIDIA/llama.cpp install)'
if ($UseSpot) {
    Write-Warn 'Spot can be evicted with 30 s notice; the relay treats eviction as "deallocated" and restarts on the next request.'
}

# --------------------------------------------------------------------------- actions
function Invoke-Or-Print {
    <#
      One code path for both modes: the dry run prints the exact az invocation that
      the execute run performs, so the printed plan cannot drift from the real one.
    #>
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [ValidateSet('none', 'json', 'tsv')][string]$Capture = 'none',
        [switch]$TolerateFailure
    )
    Write-Host ('   $ az ' + ($Arguments -join ' ')) -ForegroundColor DarkGray
    if (-not $Execute) { return $null }
    $raw = & az @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $text = ($raw | Out-String).Trim()
        if ($TolerateFailure) { Write-Warn "tolerated failure: $text"; return $null }
        throw "az $($Arguments -join ' ') failed:`n$text"
    }
    $text = ($raw | Out-String).Trim()
    switch ($Capture) {
        'json' { if ($text) { return ($text | ConvertFrom-Json) } else { return $null } }
        'tsv'  { return $text }
        default { return $null }
    }
}

Write-Step 'Build plan'
if ($Execute) {
    # --- resource group (created only if absent; never deleted) --------------
    $rg = $null
    try { $rg = Get-AzJson @('group', 'show', '-n', $ResourceGroup, '-o', 'json') } catch { $rg = $null }
    if ($rg) {
        Write-Note "resource group $ResourceGroup exists (location $($rg.location)); it will not be modified"
    } else {
        Write-Note "resource group $ResourceGroup does not exist and will be created in $Region"
        Invoke-Or-Print @('group', 'create', '-n', $ResourceGroup, '-l', $Region,
                          '--tags', 'project=av2-glm53') | Out-Null
    }
} else {
    Write-Note "resource group ${ResourceGroup}: reused if present, created if not"
}

# --- network: NSG, static IP, NIC ---------------------------------------------
Invoke-Or-Print @('network', 'nsg', 'create', '-g', $ResourceGroup, '-n', $nsgName, '-l', $Region) | Out-Null
Invoke-Or-Print @('network', 'nsg', 'rule', 'create', '-g', $ResourceGroup, '--nsg-name', $nsgName,
                  '-n', 'allow-ssh-operator', '--priority', '100', '--direction', 'Inbound',
                  '--access', 'Allow', '--protocol', 'Tcp', '--destination-port-ranges', '22',
                  '--source-address-prefixes', $OperatorCidr) | Out-Null
Invoke-Or-Print @('network', 'nsg', 'rule', 'create', '-g', $ResourceGroup, '--nsg-name', $nsgName,
                  '-n', 'allow-https-operator', '--priority', '110', '--direction', 'Inbound',
                  '--access', 'Allow', '--protocol', 'Tcp', '--destination-port-ranges', '443',
                  '--source-address-prefixes', $OperatorCidr) | Out-Null
Invoke-Or-Print @('network', 'nsg', 'rule', 'create', '-g', $ResourceGroup, '--nsg-name', $nsgName,
                  '-n', 'deny-all-inbound', '--priority', '4000', '--direction', 'Inbound',
                  '--access', 'Deny', '--protocol', '*', '--destination-port-ranges', '*',
                  '--source-address-prefixes', '*') | Out-Null

$ipAddress = Invoke-Or-Print @('network', 'public-ip', 'create', '-g', $ResourceGroup, '-n', $ipName,
                               '-l', $Region, '--sku', 'Standard', '--allocation-method', 'Static',
                               '--query', 'ipAddress', '-o', 'tsv') -Capture tsv
if ($Execute -and $ipAddress) {
    Write-Note "static public IP: $ipAddress"
} else {
    $ipAddress = '<assigned-at-create>'
    Write-Note 'static public IP is created before the VM so cloud-init can bake in the Caddy site address'
}

$caddySite = "https://$ipAddress"

Invoke-Or-Print @('network', 'vnet', 'create', '-g', $ResourceGroup, '-n', "$VmName-vnet", '-l', $Region,
                  '--address-prefixes', '10.42.0.0/16', '--subnet-name', "$VmName-subnet",
                  '--subnet-prefixes', '10.42.0.0/24') | Out-Null
Invoke-Or-Print @('network', 'nic', 'create', '-g', $ResourceGroup, '-n', $nicName, '-l', $Region,
                  '--vnet-name', "$VmName-vnet", '--subnet', "$VmName-subnet",
                  '--network-security-group', $nsgName, '--public-ip-address', $ipName) | Out-Null

# --- render cloud-init --------------------------------------------------------
Write-Step 'Render cloud-init'
if (-not (Test-Path -LiteralPath $cloudInitSource)) {
    Write-Fail "missing template: $cloudInitSource"
    exit 2
}
$template = Get-Content -LiteralPath $cloudInitSource -Raw
$rendered = $template.
    Replace('__MODEL_DIR__', $ModelDir).
    Replace('__CADDY_SITE__', $caddySite).
    Replace('__API_KEY__', $ApiKey).
    Replace('__CUDA_ARCH__', [string]$gpu.Arch).
    Replace('__CUDA_TOOLKIT__', [string]$gpu.Toolkit).
    Replace('__GPU_LAYERS__', [string]$gpu.Ngl)
$leftover = [regex]::Matches($rendered, '__[A-Z_]+__')
if ($leftover.Count -gt 0) {
    Write-Fail ('unsubstituted tokens remain: {0}' -f (($leftover | ForEach-Object { $_.Value }) -join ', '))
    exit 2
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($renderedPath, $rendered, $utf8NoBom)
Write-Note ("wrote {0} ({1} bytes, Caddy site {2})" -f $renderedPath, $rendered.Length.ToString('N0', $inv), $caddySite)
Write-Note 'weights, driver, llama.cpp build and Caddy config all come from that one file; nothing pre-exists in the VM'

# --- the VM -------------------------------------------------------------------
Write-Step 'Create the VM'
$vmArgs = @(
    'vm', 'create', '-g', $ResourceGroup, '-n', $VmName, '-l', $Region,
    '--image', 'Ubuntu2204',
    '--size', $VmSize,
    '--nics', $nicName,
    '--admin-username', 'av2ops',
    '--authentication-type', 'ssh',
    '--ssh-key-values', $SshPublicKey,
    '--os-disk-size-gb', "$OsDiskGiB",
    '--storage-sku', $DiskSku,
    '--os-disk-delete-option', 'Delete',
    '--nic-delete-option', 'Delete',
    '--custom-data', $renderedPath,
    '--tags', 'project=av2-glm53', 'mode=production', "idle-minutes=$IdleMinutes"
)
if ($UseSpot) {
    $vmArgs += @('--priority', 'Spot', '--eviction-policy', 'Deallocate', '--max-price', '-1')
}
Invoke-Or-Print $vmArgs | Out-Null

if ($AutoShutdownTime) {
    Write-Step "Auto-shutdown backstop at $AutoShutdownTime UTC"
    Write-Note 'a fixed daily stop, not an idle timer: it cannot replace the relay, it bounds a forgotten session'
    Invoke-Or-Print @('vm', 'auto-shutdown', '-g', $ResourceGroup, '-n', $VmName, '--time', $AutoShutdownTime) |
        Out-Null
}

if ($BudgetUsd -gt 0) {
    Write-Step "Spend guard: $BudgetUsd USD budget"
    $start = (Get-Date).ToUniversalTime().ToString('yyyy-MM-01')
    $end = (Get-Date).ToUniversalTime().AddYears(1).ToString('yyyy-MM-dd')
    Invoke-Or-Print @('consumption', 'budget', 'create',
                      '--budget-name', "$VmName-budget",
                      '--category', 'cost',
                      '--amount', "$BudgetUsd",
                      '--time-grain', 'Monthly',
                      '--start-date', $start,
                      '--end-date', $end) -TolerateFailure | Out-Null
    Write-Note 'budgets only alert; the hard stop is tools/glm53/wake_proxy.py --credit-cap-usd'
}

# --------------------------------------------------------------------------- summary
Write-Step 'Next steps'
if (-not $Execute) {
    Write-Host '   Dry run: nothing was created and nothing is billing.' -ForegroundColor Yellow
    Write-Host '   Re-run with -Execute to build exactly the resources listed above.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '   Prove the relay logic offline first (no Azure needed):' -ForegroundColor Gray
    Write-Host '     python tools\glm53\wake_proxy.py --self-test' -ForegroundColor Gray
    Write-Host ''
    Write-Host '   After -Execute, on this machine (the AV2 host):' -ForegroundColor Gray
    Write-Host ("     python tools\glm53\wake_proxy.py --upstream https://<ip>/v1 --upstream-key <key> ``" ) -ForegroundColor Gray
    Write-Host ("       --relay-key <key> --resource-group $ResourceGroup --vm-name $VmName ``") -ForegroundColor Gray
    Write-Host ("       --idle-minutes $IdleMinutes --hourly-usd $hourlyText --credit-cap-usd $budgetText --warn-usd $warnText") -ForegroundColor Gray
    Write-Host '     .\tools\glm53\connect_av2.ps1 -BaseUrl http://127.0.0.1:8430/v1 -ApiKey <relay key> -Apply' -ForegroundColor Gray
    exit 0
}

Write-Host ("   public IP      : {0}" -f $ipAddress) -ForegroundColor Green
Write-Host ("   OpenAI surface : {0}/v1  (Caddy, internal CA)" -f $caddySite)
Write-Host ("   llama api key  : {0}" -f $ApiKey) -ForegroundColor Yellow
Write-Host  '   ^ shown once here; it also lives in /etc/av2-glm53/env on the box (0600 root)'
Write-Host ''
Write-Host '   Bootstrap takes ~15-30 min on first boot: NVIDIA driver, CUDA toolkit, 71.5 GiB of'
Write-Host '   weights, then the llama.cpp build.  The VM bills while that happens.'
Write-Host ("   Watch it : ssh av2ops@{0} 'sudo journalctl -u av2-glm53-bootstrap -f'" -f $ipAddress)
Write-Host ''
Write-Host '   Start the relay here (keep it running; it is the wake button, the spend cap and the' -ForegroundColor Gray
Write-Host '   only writer of the cost ledger.  --idle-minutes 0 means it never stops the GPU):' -ForegroundColor Gray
Write-Host ("     python tools\glm53\wake_proxy.py --upstream {0}/v1 --upstream-key <key> ``" -f $caddySite) -ForegroundColor Gray
Write-Host ("       --relay-key <key> --resource-group {0} --vm-name {1} --idle-minutes {2} --hourly-usd {3} ``" -f $ResourceGroup, $VmName, $IdleMinutes, $hourlyText) -ForegroundColor Gray
Write-Host ("       --credit-cap-usd {0} --warn-usd {1}" -f $budgetText, $warnText) -ForegroundColor Gray
Write-Host ("     .\tools\glm53\connect_av2.ps1 -BaseUrl http://127.0.0.1:8430/v1 -ApiKey <relay key> -Apply") -ForegroundColor Gray
Write-Host ''
Write-Host '   Health check that is not a lie:' -ForegroundColor Gray
Write-Host '     curl http://127.0.0.1:8430/__relay/state    # vm_state, estimated_usd, idle countdown' -ForegroundColor Gray
Write-Host '     /v1/models answers from cache and says nothing about the GPU -- that is deliberate,' -ForegroundColor Gray
Write-Host '     because AV2 probes it with a 20 s timeout and a cold A100 takes minutes.' -ForegroundColor Gray
Write-Host ''
Write-Host '   Tear down when the credit gets thin:' -ForegroundColor Gray
Write-Host ("     .\tools\glm53\azure-teardown.ps1 -ResourceGroup {0} -VmName {1} -Execute" -f $ResourceGroup, $VmName) -ForegroundColor Gray
Write-Host ''
Write-Warn 'The pasted Hugging Face token was not used: that repo is public and ungated.'
Write-Warn 'Revoke it anyway (huggingface.co/settings/tokens), along with the Akamai and Render keys.'

