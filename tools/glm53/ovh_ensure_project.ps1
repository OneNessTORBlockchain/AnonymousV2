# ovh_ensure_project.ps1
# Guarantees a Public Cloud project exists, because that is the only thing a GPU
# instance can live in and it cannot be created directly: POST /cloud/project is
# 405 Method Not Allowed. Projects come from the order flow, and a checked-out
# order that nobody paid - even at $0.00 CAD - leaves /cloud/project answering
# [] forever. provision_ovh.ps1 then reports "no project", which reads like a
# permission bug and is not one.
#
#   .\ovh_ensure_project.ps1                 # read-only: projects + pending orders
#   .\ovh_ensure_project.ps1 -Pay            # pay every notPaid order, then wait
#   .\ovh_ensure_project.ps1 -Pay -WaitMinutes 20
#
# Needs GET /cloud/project plus GET /me/order, GET /me/order/* and
# POST /me/order/* - .\ovh_mint_ck.ps1 requests all of them.
#
# Exit codes: 0 = a project exists, 1 = error, 2 = no project and nothing payable,
#             3 = payment attempted, no project before the timeout.

[CmdletBinding()]
param(
    [switch]$Pay,
    [int]$WaitMinutes = 10
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'ovh_api.ps1')

# Not Write-Error: $ErrorActionPreference is Stop, so Write-Error would rethrow
# inside the catch block and lose the exit code.
function Write-Fail {
    param([string]$Msg)
    Write-Host $Msg -ForegroundColor Red
}

function Get-ProjectList {
    $r = Invoke-OvhApi -Method 'GET' -Path '/cloud/project'
    if (-not $r.Ok) {
        if ($r.Message -eq 'This credential is not valid') {
            throw ('The consumer key is not approved yet. Open the validationUrl printed by ' +
                   '.\ovh_mint_ck.ps1, log in to OVH, approve, then set $env:OVH_CONSUMER_KEY.')
        }
        throw "GET /cloud/project failed: HTTP $($r.Status) $($r.Body)"
    }
    # ConvertFrom-Json turns "[]" into $null and "[x]" into a bare string, so wrap.
    return @($r.Json | Where-Object { $_ })
}

function Get-OrderList {
    # /me/order is a different grant from /cloud/project. A key minted before this
    # script existed has neither the read nor the pay right, and the fix is a new
    # key, not a retry.
    $r = Invoke-OvhApi -Method 'GET' -Path '/me/order'
    if (-not $r.Ok) {
        if ($r.ErrorCode -eq 'NOT_GRANTED_CALL') {
            throw ('This consumer key cannot read GET /me/order, so a pending project order is ' +
                   'invisible and unpayable. Re-mint it with .\ovh_mint_ck.ps1 and approve the URL.')
        }
        throw "GET /me/order failed: HTTP $($r.Status) $($r.Body)"
    }
    $rows = New-Object System.Collections.ArrayList
    foreach ($raw in @($r.Json)) {
        $oid = [string]$raw
        if (-not $oid) { continue }
        $o = Invoke-OvhApi -Method 'GET' -Path "/me/order/$oid"
        $st = Invoke-OvhApi -Method 'GET' -Path "/me/order/$oid/status"
        $price = Get-Field (Get-Field $o.Json 'priceWithTax' $null) 'text' ''
        [void]$rows.Add([pscustomobject]@{
            OrderId = $oid
            Date    = [string](Get-Field $o.Json 'date' '')
            Price   = [string]$price
            Status  = [string]$st.Json
            PayUrl  = [string](Get-Field $o.Json 'url' '')
        })
    }
    return @($rows | Sort-Object -Property Date -Descending)
}

function Show-Projects {
    param($Projects)
    Write-Host 'Public Cloud project(s) on this account:'
    foreach ($p in $Projects) { Write-Host "  $p" }
    Write-Host 'next: .\provision_ovh.ps1 -List'
}

try {
    $projects = @(Get-ProjectList)
    if ($projects.Count -gt 0) {
        Show-Projects $projects
        exit 0
    }

    Write-Host 'No Public Cloud project: /cloud/project answers [].'
    Write-Host 'POST /cloud/project is 405, so a project can only arrive through an order.'

    $orders = @(Get-OrderList)
    if (-not $orders) {
        Write-Host ''
        Write-Host 'No order on this account either. Create the project at ca.ovh.com/manager'
        Write-Host '(Public Cloud > Create a project). It is a $0 order and returns as a'
        Write-Host 'project id, not an instance.'
        exit 2
    }

    Write-Host ''
    $orders | Format-Table -Property OrderId, Date, Price, Status, PayUrl -AutoSize

    $unpaid = @($orders | Where-Object { $_.Status -eq 'notPaid' })
    if (-not $unpaid) {
        Write-Host 'Nothing is waiting for payment, so the project is stuck somewhere else;'
        Write-Host 'ca.ovh.com/manager > Orders shows the current state of each order.'
        exit 2
    }

    if (-not $Pay) {
        Write-Host 'Order(s) waiting for payment. Even $0.00 CAD has to be accepted:'
        foreach ($o in $unpaid) { if ($o.PayUrl) { Write-Host "  $($o.PayUrl)" } }
        Write-Host 'Re-run with -Pay to settle them from here.'
        exit 2
    }

    foreach ($o in $unpaid) {
        Write-Host ("paying order {0} ({1}) ..." -f $o.OrderId, $o.Price)
        $p = Invoke-OvhApi -Method 'POST' -Path "/me/order/$($o.OrderId)/pay" -Body ([ordered]@{})
        if ($p.Ok) {
            Write-Host '  paid'
        } else {
            Write-Fail ("  pay refused: HTTP {0} {1} {2}" -f $p.Status, $p.ErrorCode, $p.Message)
            Write-Fail ("  body: {0}" -f $p.Body)
            if ($o.PayUrl) { Write-Host "  accept it in a browser instead: $($o.PayUrl)" }
        }
    }

    # A project appears asynchronously after payment; polling beats guessing.
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 20
        $projects = @(Get-ProjectList)
        if ($projects.Count -gt 0) {
            Show-Projects $projects
            exit 0
        }
        Write-Host ("  still empty at {0}" -f (Get-Date -Format 'HH:mm:ss'))
    }
    Write-Fail ("no project after {0} minutes; OVH creates it asynchronously once paid." -f $WaitMinutes)
    exit 3
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
