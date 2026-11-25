# Anonymous V2 - OVHcloud Public Cloud GPU instance for GLM-5.3-Flash-Uncensored (IQ1_S).
#
#   .\provision_ovh.ps1 -List                       # GPU flavors + quota, no creation
#   .\provision_ovh.ps1                             # dry run: chosen plan/region + API calls
#   .\provision_ovh.ps1 -Execute -BurstHours 16 -OperatorCidr 203.0.113.7/32
#   .\provision_ovh.ps1 -Destroy -InstanceId 5f2... # stop billing
#
# Credentials come from the environment and are never printed or written to disk:
#   OVH_APP_KEY, OVH_APP_SECRET, OVH_CONSUMER_KEY
# Mint a consumer key with .\ovh_mint_ck.ps1 - it prints a URL you approve in a
# browser, and the key is inert (NOT_GRANTED_CALL) until you do.
#
# Endpoint defaults to https://ca.api.ovh.com/1.0 because that is where this
# application key is registered: EU and US answer "This application key is invalid".
# Override with $env:OVH_ENDPOINT if the account lives elsewhere.
#
# Why quota is printed first: OVH Public Cloud GPU flavors carry a per-flavor
# `quota` of instances the project may run. A project without GPU quota returns a
# generic 403/400 on create, so this script refuses to call create at all unless a
# GPU flavor reports quota >= 1. That failure mode is account-level, exactly like
# the Akamai plan gate, and only OVH support can raise it.
[CmdletBinding(DefaultParameterSetName = 'Plan')]
param(
    [Parameter(ParameterSetName = 'List')]
    [switch]$List,

    [Parameter(ParameterSetName = 'Destroy')]
    [switch]$Destroy,

    [Parameter(ParameterSetName = 'Destroy')]
    [string]$InstanceId = '',

    [string]$ProjectId = '',
    [string]$Region = '',
    [string]$Plan = '',
    [string]$Image = '',
    [string]$Name = 'av2-glm53',
    [string]$SshKeyPath = "$HOME\.ssh\id_ed25519.pub",
    [string]$SshPublicKey = '',
    [string]$OperatorCidr = '',
    [double]$BurstHours = 0,
    [int]$Ctx = 16384,
    [int]$Parallel = 4,
    [double]$WeightsGb = 71.5,
    [int]$MinVramGb = 80,
    [switch]$IgnoreQuota,
    [int]$WaitMinutes = 40,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Costs and sizes are read by a human on a French Windows, where [double] 6 renders
# as "6,00" and mixes with the "." thousands separator in the same table.
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

. (Join-Path $PSScriptRoot 'ovh_api.ps1')

# VRAM per card, inferred from the flavor id. OVH exposes vcpus/ram/disk/quota but
# not VRAM, so this table is the only source of that number and is deliberately
# explicit about which ids it knows. Unknown ids report 0 and are never auto-picked.
$VramByPattern = [ordered]@{
    'h100'    = 80
    'h200'    = 141
    'a100'    = 80
    'l40s'    = 48
    'l40'     = 48
    'l4'      = 24
    'a10'     = 24
    'rtx6000' = 24
    'rtx8000' = 48
    'v100s'   = 32
    'v100'    = 16
}

function Get-CardCount {
    param([string]$Name)
    if ($Name -match '(\d+)\s*x') { return [int]$Matches[1] }
    return 1
}

function Get-VramGb {
    param([string]$FlavorId)
    $id = $FlavorId.ToLowerInvariant()
    foreach ($k in $VramByPattern.Keys) {
        if ($id -match [regex]::Escape($k)) { return $VramByPattern[$k] }
    }
    return 0
}

function Test-GpuFlavor {
    param([string]$FlavorId)
    return ($FlavorId -match '(?i)gpu|h100|h200|a100|l40|l4-|a10-|rtx|tesla|v100')
}

# Runs only on the failure path, when /cloud/project came back empty. An empty
# list has two very different causes and they need different actions: no order
# was ever placed (cart still holds the item), or the order exists but nobody
# accepted its contracts (`GET /order/cart/{id}` then 404s with "This cart has
# already been checkout"). Saying which one saves a support ticket.
function Get-ProjectOrderHint {
    $lines = New-Object System.Collections.ArrayList
    $carts = Invoke-OvhApi -Method 'GET' -Path '/order/cart'
    if (-not $carts.Ok) { return $lines }
    foreach ($raw in @($carts.Json)) {
        $cid = [string]$raw
        if (-not $cid) { continue }
        $meta = Invoke-OvhApi -Method 'GET' -Path "/order/cart/$cid"
        if ($meta.Ok) {
            $desc = [string](Get-Field $meta.Json 'description' '')
            $items = @(Get-Field $meta.Json 'items' @())
            if ($items.Count -eq 0) {
                [void]$lines.Add("  cart $cid ($desc) is empty")
                continue
            }
            foreach ($it in $items) {
                $item = Invoke-OvhApi -Method 'GET' -Path "/order/cart/$cid/item/$it"
                if (-not $item.Ok) { continue }
                $set = Get-Field $item.Json 'settings' $null
                $plan = if ($set) { [string](Get-Field $set 'planCode' '') } else { '' }
                [void]$lines.Add("  cart $cid ($desc) still holds $plan - never checked out")
                [void]$lines.Add("    POST /order/cart/$cid/checkout would create the order")
            }
        } elseif ($meta.Status -eq 404) {
            [void]$lines.Add("  cart $cid was already checked out, but the order is not confirmed")
            [void]$lines.Add('    open ca.ovh.com/manager > Orders, accept the contracts and pay;')
            [void]$lines.Add('    /cloud/project stays empty until the order completes')
        }
    }
    return $lines
}

function Resolve-Project {
    param([string]$Wanted)
    $projects = Get-OvhCloudProjects
    if (-not $projects) {
        $msg = ('No Public Cloud project on this account. OVH GPU instances exist only ' +
                'inside a Public Cloud project; create one at ca.ovh.com/manager and rerun.')
        # One API sweep, then either the concrete hint or the "already checked out"
        # fallback - calling Get-ProjectOrderHint twice would double the cart calls.
        $hints = @(Get-ProjectOrderHint)
        if ($hints.Count -eq 0) {
            $msg += [Environment]::NewLine +
                    '  no open cart either - if a project order was already checked out,' +
                    ' it is unpaid: ca.ovh.com/manager > Orders'
        } else {
            foreach ($h in $hints) { $msg += [Environment]::NewLine + $h }
        }
        throw $msg
    }
    if ($Wanted) {
        if ($projects -notcontains $Wanted) {
            throw "Project '$Wanted' not visible to this consumer key. Visible: $($projects -join ', ')"
        }
        return $Wanted
    }
    if (@($projects).Count -gt 1) {
        throw "Multiple projects visible; pass -ProjectId. Visible: $($projects -join ', ')"
    }
    return @($projects)[0]
}

# Get-Field moved to ovh_api.ps1, because ovh_ensure_project.ps1 needs the same
# StrictMode-safe read and both scripts dot-source that file.

function Get-GpuFlavorTable {
    param([string]$Proj, [string]$OnlyRegion)
    $regions = @()
    if ($OnlyRegion) {
        $regions = @($OnlyRegion)
    } else {
        $r = Invoke-OvhApi -Method 'GET' -Path "/cloud/project/$Proj/region"
        if (-not $r.Ok) { throw "GET /cloud/project/$Proj/region failed: HTTP $($r.Status) $($r.Body)" }
        # This route answers a bare string array (["BHS","GRA",...]), not the
        # cloud.region objects the docs describe. Reading .name off a string yields
        # $null, which silently skipped every region and made -List claim "no GPU
        # flavor" for projects that had them. Accept both shapes.
        $regions = @($r.Json | ForEach-Object {
            if ($_ -is [string]) { $_ }
            elseif ((Get-Field $_ 'status' 'UP') -eq 'UP') { [string](Get-Field $_ 'name' '') }
        } | Where-Object { $_ })
    }
    $rows = New-Object System.Collections.ArrayList
    foreach ($rg in $regions) {
        if (-not $rg) { continue }
        $f = Invoke-OvhApi -Method 'GET' -Path "/cloud/project/$Proj/flavor?region=$rg"
        if (-not $f.Ok) { continue }
        foreach ($fl in @($f.Json)) {
            $id = [string](Get-Field $fl 'name' '')
            if (-not (Test-GpuFlavor $id)) { continue }
            $cards = Get-CardCount $id
            $vram = Get-VramGb $id
            # OVH sends ram and disk in GB, not the megabytes the field names
            # suggest: h200-1920-eph is {"ram":1920,"disk":400}. Dividing by 1024
            # printed a 1.9 TB machine as "2 GB RAM".
            [void]$rows.Add([pscustomobject]@{
                Region    = $rg
                Name      = $id
                Id        = (Get-Field $fl 'id' '')
                Vcpu      = (Get-Field $fl 'vcpus' 0)
                RamGb     = [int](Get-Field $fl 'ram' 0)
                DiskGb    = (Get-Field $fl 'disk' 0)
                Gpus      = $cards
                VramGb    = ($vram * $cards)
                Quota     = (Get-Field $fl 'quota' 0)
                Available = (Get-Field $fl 'available' $true)
            })
        }
    }
    return $rows
}

function Select-Flavor {
    param($Rows, [string]$Wanted, [int]$MinVram)
    if ($Wanted) {
        $hit = @($Rows | Where-Object { $_.Name -eq $Wanted -or $_.Id -eq $Wanted })
        if (-not $hit) { throw "Flavor '$Wanted' is not offered as a GPU flavor in any UP region." }
        return $hit[0]
    }
    $usable = @($Rows | Where-Object { [int]$_.Quota -ge 1 -and $_.Available -ne $false })
    if (-not $usable) { return $null }
    $fits = @($usable | Where-Object { $_.VramGb -ge $MinVram } |
        Sort-Object -Property VramGb, RamGb -Descending)
    if ($fits) { return $fits[0] }
    # Nothing satisfies -MinVramGb; take the largest shape and let the caller say so.
    return @($usable | Sort-Object -Property VramGb, RamGb -Descending)[0]
}

function Resolve-Image {
    param([string]$Proj, [string]$Rg, [string]$Wanted)
    $r = Invoke-OvhApi -Method 'GET' -Path "/cloud/project/$Proj/image?region=$Rg"
    if (-not $r.Ok) { throw "GET image?region=$Rg failed: HTTP $($r.Status) $($r.Body)" }
    $all = @($r.Json)
    if ($Wanted) {
        $hit = @($all | Where-Object { $_.id -eq $Wanted -or $_.name -eq $Wanted })
        if (-not $hit) { throw "Image '$Wanted' not available in $Rg." }
        return $hit[0]
    }
    foreach ($want in @('Ubuntu 24.04', 'Ubuntu 22.04')) {
        $hit = @($all | Where-Object { (Get-Field $_ 'name' '') -like "$want*" -and
                                       (Get-Field $_ 'status' 'active') -eq 'active' })
        if ($hit) { return @($hit | Sort-Object { (Get-Field $_ 'name') })[0] }
    }
    throw "No Ubuntu image found in $Rg; pass -Image."
}

function Resolve-SshKey {
    param([string]$Proj)
    $pub = $SshPublicKey
    if (-not $pub) {
        if (-not (Test-Path -LiteralPath $SshKeyPath)) {
            throw "SSH public key not found at $SshKeyPath. Pass -SshPublicKey '<key>' or -SshKeyPath."
        }
        $pub = (Get-Content -LiteralPath $SshKeyPath -Raw).Trim()
    }
    if ($pub -notmatch '^(ssh-|ecdsa-)') { throw 'SshPublicKey does not look like an OpenSSH public key.' }
    $r = Invoke-OvhApi -Method 'GET' -Path "/cloud/project/$Proj/sshkey"
    if ($r.Ok) {
        foreach ($k in @($r.Json)) {
            if ([string](Get-Field $k 'publicKey' '').Trim() -eq $pub) { return $k }
        }
    }
    $body = [ordered]@{
        name      = ('av2-' + (Get-Date -Format 'yyyyMMddHHmmss'))
        publicKey = $pub
    }
    $c = Invoke-OvhApi -Method 'POST' -Path "/cloud/project/$Proj/sshkey" -Body $body
    if (-not $c.Ok) { throw "POST /sshkey failed: HTTP $($c.Status) $($c.Body)" }
    Write-Host ("registered SSH key '{0}'" -f (Get-Field $c.Json 'name' '?'))
    return $c.Json
}

function Write-Fail {
    param([string]$Msg)
    # Not Write-Error: $ErrorActionPreference is Stop, so Write-Error would rethrow
    # inside the catch block and lose the exit code.
    Write-Host $Msg -ForegroundColor Red
}

# ------------------------------------------------------------------ main
try {
    if ($Destroy) {
        if (-not $InstanceId) { throw '-Destroy needs -InstanceId <uuid>.' }
        $proj = Resolve-Project $ProjectId
        $d = Invoke-OvhApi -Method 'DELETE' -Path "/cloud/project/$proj/instance/$InstanceId"
        if (-not $d.Ok) { throw "DELETE instance failed: HTTP $($d.Status) $($d.Body)" }
        Write-Host "delete requested for $InstanceId; billing stops when it leaves DELETING."
        exit 0
    }

    $proj = Resolve-Project $ProjectId
    Write-Host "project    : $proj"
    Write-Host "endpoint   : $(Get-OvhEndpoint)"

    $rows = Get-GpuFlavorTable -Proj $proj -OnlyRegion $Region

    if ($List) {
        if (-not $rows) {
            Write-Host 'No GPU flavor visible in any UP region for this project.'
            exit 2
        }
        $rows | Sort-Object -Property Region, VramGb -Descending |
            Format-Table -Property Region, Name, Gpus, VramGb, Vcpu, RamGb, DiskGb, Quota, Available -AutoSize
        $withQuota = @($rows | Where-Object { [int]$_.Quota -ge 1 })
        Write-Host ("GPU flavors with quota>=1: {0} of {1}" -f $withQuota.Count, $rows.Count)
        if (-not $withQuota) {
            Write-Fail 'GPU quota is 0 on every flavor. A create call would be refused.'
            Write-Fail 'Fix: ca.ovh.com/manager > Public Cloud > (project) > Support > open a ticket asking for GPU instance quota,'
            Write-Fail 'and name the flavor you want (the largest h100/a100/l4 id listed above).'
            exit 3
        }
        exit 0
    }

    if (-not $rows) {
        throw 'No GPU flavor visible for this project. Run -List; if that is empty the project is not GPU-entitled.'
    }

    $chosen = Select-Flavor -Rows $rows -Wanted $Plan -MinVram $MinVramGb
    if (-not $chosen -and $IgnoreQuota) {
        # `quota` is a per-flavor number OVH reports for the project, and 0 normally
        # means create is refused. -IgnoreQuota sends the create anyway so OVH answers
        # for real - that is the only way to tell a stale/informational quota field
        # from a hard account gate, without guessing from the field alone.
        $pref = @(@($rows) | Where-Object { $_.Available -ne $false } |
            Sort-Object -Property VramGb, RamGb -Descending)
        if (-not $pref) { $pref = @(@($rows) | Sort-Object -Property VramGb, RamGb -Descending) }
        $chosen = @($pref)[0]
        Write-Host ("-IgnoreQuota: quota is 0 on every GPU flavor; trying {0} in {1} anyway." -f
            $chosen.Name, $chosen.Region) -ForegroundColor Yellow
    }
    if (-not $chosen) {
        throw ('GPU quota is 0 for every GPU flavor in this project, so create would be refused. ' +
               'This is account level, not a script problem: open an OVH support ticket for GPU quota. ' +
               'Nothing was created and nothing was billed.')
    }
    if ($chosen.VramGb -lt $MinVramGb) {
        Write-Host ("note: {0} exposes {1} GB VRAM, under -MinVramGb {2}; the serve script splits " +
                    "weights with --n-cpu-moe instead of pretending they fit." -f
                    $chosen.Name, $chosen.VramGb, $MinVramGb) -ForegroundColor Yellow
    }
    $regionName = if ($Region) { $Region } else { $chosen.Region }
    $img = Resolve-Image -Proj $proj -Rg $regionName -Wanted $Image

    $cinitPath = Join-Path $PSScriptRoot 'cloud-init-ovh.yaml'
    if (-not (Test-Path -LiteralPath $cinitPath)) { throw "Missing cloud-init template: $cinitPath" }
    $cinit = Get-Content -LiteralPath $cinitPath -Raw
    $cidr = if ($OperatorCidr) { $OperatorCidr } else { '0.0.0.0/0' }
    $cinit = $cinit.Replace('__OPERATOR_CIDR__', $cidr).
        Replace('__CTX__', [string]$Ctx).
        Replace('__PARALLEL__', [string]$Parallel)
    $userData = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cinit))

    $body = [ordered]@{
        name           = $Name
        region         = $regionName
        flavorId       = $chosen.Id
        imageId        = $img.id
        sshKeyId       = '<registered on execute>'
        monthlyBilling = $false
        userData       = ('<cloud-init base64, {0} chars>' -f $userData.Length)
    }

    Write-Host ''
    Write-Host 'plan'
    Write-Host ("  flavor   : {0}  ({1} GPU x {2} GB VRAM, {3} vCPU, {4} GB RAM, {5} GB disk, quota {6})" -f `
        $chosen.Name, $chosen.Gpus, ($chosen.VramGb / [math]::Max(1, $chosen.Gpus)), $chosen.Vcpu, `
        $chosen.RamGb, $chosen.DiskGb, $chosen.Quota)
    Write-Host ("  region   : {0}" -f $regionName)
    Write-Host ("  image    : {0}" -f (Get-Field $img 'name' '?'))
    Write-Host '  billing  : hourly (monthlyBilling=false)'
    Write-Host ("  operator : {0} -> server port 8080 only" -f $cidr)

    if (-not $Execute) {
        Write-Host ''
        Write-Host 'DRY RUN - nothing created. Would send:'
        Write-Host ('  POST /cloud/project/{0}/sshkey    (idempotent, registers your public key)' -f $proj)
        Write-Host ('  POST /cloud/project/{0}/instance  {1}' -f $proj, ($body | ConvertTo-Json -Compress))
        Write-Host ''
        Write-Host 'Re-run with -Execute to create it (hourly billing starts immediately).'
        exit 0
    }

    $key = Resolve-SshKey -Proj $proj
    $body.sshKeyId = [string](Get-Field $key 'id' '')
    $body.userData = $userData

    Write-Host ''
    Write-Host 'creating instance ...'
    $created = Invoke-OvhApi -Method 'POST' -Path "/cloud/project/$proj/instance" -Body $body
    if (-not $created.Ok) {
        Write-Fail ("POST instance failed: HTTP {0} {1}" -f $created.Status, $created.Message)
        Write-Fail $created.Body
        Write-Fail 'If this mentions quota or capability it is an account-level GPU gate: open an OVH support ticket.'
        exit 1
    }
    $iid = [string](Get-Field $created.Json 'id' '')
    Write-Host "instance id: $iid"

    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    $ip = ''
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        $g = Invoke-OvhApi -Method 'GET' -Path "/cloud/project/$proj/instance/$iid"
        if (-not $g.Ok) { continue }
        $st = (Get-Field $g.Json 'status' '')
        foreach ($a in @(Get-Field $g.Json 'ipAddresses' @())) {
            if ($a -notmatch '^(10\.|172\.(1[6-9]|2\d|3[01])\.|192\.168\.)') { $ip = [string]$a; break }
        }
        Write-Host ("  {0,-12} {1}" -f $st, $ip)
        if ($st -eq 'ACTIVE' -and $ip) { break }
    }

    Write-Host ''
    if ($ip) {
        Write-Host "ready: ssh root@$ip"
        Write-Host 'then : bash /opt/glm53/fetch.sh && bash /opt/glm53/serve.sh'
    } else {
        Write-Host "instance $iid created but no public IP yet; check the OVH manager." -ForegroundColor Yellow
    }
    if ($BurstHours -gt 0) {
        Write-Host ("burst window {0} h - stop billing when done with:" -f $BurstHours)
    }
    Write-Host "  .\provision_ovh.ps1 -Destroy -InstanceId $iid"
    exit 0
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}
