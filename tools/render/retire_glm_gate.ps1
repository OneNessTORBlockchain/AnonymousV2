# Anonymous V2 - delete the Render services that served the self-hosted GLM brain.
#
#   .\tools\render\retire_glm_gate.ps1                    # dry run: list them, delete nothing
#   .\tools\render\retire_glm_gate.ps1 -Execute           # delete them (needs a Render API key)
#   .\tools\render\retire_glm_gate.ps1 -Execute -ServiceId srv-xxxxxxxxxxxx
#
# The two ids come from docs/GPU-PROVIDER-OPTIONS.md 6d, read back from the Render
# API on 23 Nov 2026:
#
#   anonymous-ollama-gate  srv-dc1ufdifngtc738n1qdg  web_service      0.5c-512mb  (~$7/mo)
#   anonymous-ollama       srv-dbofib8u01pc73f48tm0  private_service  12c-24g     (~$450/mo)
#
# The brain is a CPU-only box: a 10.3 GB IQ2_M GGUF at 4 096 tokens of context, a
# warm turn in 41 s, and every AV2 lane died on "request (4775 tokens) exceeds the
# available context size (4096 tokens)".  It billed by the hour whether it answered
# or not, so AV2 now runs on Venice (tools\connect_venice.ps1) and this is the
# command that stops the meter.  Deleting a service also removes its disk
# (`dsk-dbofib8u01pc73f48tvg`); if a disk line survives in the dashboard, remove it
# there - the API can only delete services.
#
# Nothing here touches Venice, the repo or the sandbox: it is one API surface.
[CmdletBinding()]
param(
    # Falls back to $env:RENDER_API_KEY.  Render shows a key once, at creation:
    # Dashboard -> Account Settings -> API Keys.  The key used earlier in this
    # workspace is revoked (it answers 401) and cannot be recovered.
    [string]$ApiKey = '',
    [string[]]$ServiceId = @('srv-dc1ufdifngtc738n1qdg', 'srv-dbofib8u01pc73f48tm0'),
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
$api = 'https://api.render.com/v1'
if (-not $ApiKey) { $ApiKey = $env:RENDER_API_KEY }

Write-Host ''
Write-Host 'AV2 - retire the self-hosted GLM brain on Render' -ForegroundColor Cyan
Write-Host ("  targets : {0}" -f ($ServiceId -join ', '))
Write-Host '  costs   : 12c-24g private_service ~450 USD/month, 0.5c-512mb gate ~7 USD/month'
Write-Host '  replaced: https://api.venice.ai/api/v1 (tools\connect_venice.ps1)'

if (-not $ApiKey) {
    Write-Host ''
    Write-Host 'No Render API key in this shell (-ApiKey or $env:RENDER_API_KEY).' -ForegroundColor Yellow
    Write-Host 'Mint one: Render Dashboard -> Account Settings -> API Keys -> Create (copy it once).' -ForegroundColor Yellow
    Write-Host 'Then either re-run this script, or paste the equivalent by hand:' -ForegroundColor Yellow
    foreach ($id in $ServiceId) {
        Write-Host ("  curl.exe -X DELETE -H `"Authorization: Bearer <key>`" {0}/services/{1}" -f $api, $id) -ForegroundColor DarkGray
    }
    if ($Execute) { throw 'Nothing was deleted: -Execute needs a Render API key.' }
    return
}

$headers = @{ Authorization = "Bearer $ApiKey"; Accept = 'application/json' }
try {
    $items = (Invoke-RestMethod -Uri "$api/services?limit=100" -Headers $headers -TimeoutSec 60).items
} catch {
    throw ("Render rejected the key ({0}). Mint a new one at Dashboard -> Account Settings -> API Keys." -f $_.Exception.Message)
}
$live = @()
foreach ($item in $items) {
    $service = if ($item.service) { $item.service } else { $item }
    $live += $service
}
Write-Host ''
Write-Host ("workspace has {0} service(s)" -f $live.Count) -ForegroundColor DarkGray
$live | Sort-Object name | ForEach-Object {
    $mark = if ($ServiceId -contains $_.id) { '*' } else { ' ' }
    Write-Host (" {0} {1,-24} {2,-16} {3,-10} {4,-10} {5}" -f $mark, $_.name, $_.id, $_.type, $_.plan, $_.suspended)
}

$found = 0
$deleted = 0
foreach ($id in $ServiceId) {
    $service = $live | Where-Object { $_.id -eq $id } | Select-Object -First 1
    if (-not $service) {
        Write-Host ("  {0} is already gone" -f $id) -ForegroundColor DarkGray
        continue
    }
    $found++
    if (-not $Execute) {
        Write-Host ("  would DELETE {0} ({1}, {2})" -f $service.name, $service.type, $service.plan) -ForegroundColor Yellow
        continue
    }
    try {
        $response = Invoke-WebRequest -Uri "$api/services/$id" -Method Delete -Headers $headers -UseBasicParsing -TimeoutSec 90
        if ($response.StatusCode -in 200, 202, 204) {
            $deleted++
            Write-Host ("  deleted {0} ({1}) -> HTTP {2}" -f $service.name, $id, $response.StatusCode) -ForegroundColor Green
        } else {
            Write-Host ("  {0} answered HTTP {1}" -f $id, $response.StatusCode) -ForegroundColor Red
        }
    } catch {
        Write-Host ("  deleting {0} failed: {1}" -f $id, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ''
if (-not $Execute) {
    if ($found -eq 0) {
        Write-Host 'Both services are already deleted; nothing is billing for them.' -ForegroundColor Green
    } else {
        Write-Host ("Dry run: {0} service(s) would be deleted, nothing was.  Re-run with -Execute." -f $found) -ForegroundColor Yellow
    }
    return
}
if ($deleted -eq $found -and $found -gt 0) {
    Write-Host ("Meter stopped: {0} service(s) deleted.  Check the dashboard once for a leftover disk." -f $deleted) -ForegroundColor Green
} elseif ($found -eq 0) {
    Write-Host 'Both services were already gone; nothing is billing for them.' -ForegroundColor Green
} else {
    throw ("Only {0} of {1} services were deleted; the rest are still billing - see the lines above." -f $deleted, $found)
}
