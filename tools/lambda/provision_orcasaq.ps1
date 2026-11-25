# Anonymous V2 - Lambda Cloud provisioning for OrcaSAQ-2-Cyber-27B-Uncensored.
#
#   $env:LAMBDA_API_KEY = '<API key from https://cloud.lambda.com/api_keys>'
#   .\provision_orcasaq.ps1 -Action Types                      # sizes, prices, live capacity
#   .\provision_orcasaq.ps1 -Action Create -OperatorCidr 173.177.72.31/32
#   .\provision_orcasaq.ps1 -Action Wait                        # IP + /opt/av2/READY
#   .\provision_orcasaq.ps1 -Action Logs                        # bootstrap tail + credentials
#   .\provision_orcasaq.ps1 -Action Destroy                     # the only thing that stops billing
#
# Why Lambda at all: it is the one provider that is genuinely **postpaid with no
# deposit** - a card on file, a $10 *refundable* pre-authorisation, then "weekly
# invoices for the previous week's usage" (docs.lambda.com/public-cloud/billing).
# So the box runs today and the first real bill lands ~7 days later. The RTX
# A6000 48 GB is $1.09/h: 70 h = $76.30, 140 h = $152.60, both inside the $250
# budget. Thunder Compute is 3x cheaper per hour ($0.35) but auto-charges the
# card at usage thresholds, i.e. money leaves today, not next week.
#
# What Create does, in order (each step is idempotent):
#   1. GET  /instance-types          -> finds gpu_1x_a6000 and a region that
#                                       actually reports capacity right now
#   2. GET/POST /ssh-keys            -> uploads ~/.ssh/av2-lambda-orcasaq.pub
#   3. PATCH /firewall-rulesets/global -> tcp/8080 from the operator CIDR only
#   4. POST /instance-operations/launch -> cloud-init runs bootstrap.sh as root
# Nothing above needs a browser except the one-time account + card setup.
[CmdletBinding()]
param(
    [ValidateSet('Create', 'Status', 'Wait', 'Destroy', 'List', 'Logs', 'Types', 'Firewall')]
    [string]$Action = 'Status',
    # gpu_1x_a6000 = 1x RTX A6000 48 GB, 14 vCPU, 100 GiB RAM, 512 GiB SSD, $1.09/h.
    # If the name ever changes, any type whose GPU description contains "A6000" is used.
    [string]$InstanceType = 'gpu_1x_a6000',
    # 'auto' = first region that currently reports capacity for the chosen type.
    [string]$Region = 'auto',
    [string]$OperatorCidr = '173.177.72.31/32',
    [string]$Name = 'av2-orcasaq',
    [string]$ApiKey = $env:LAMBDA_API_KEY,
    # Optional: run the canonical gated repo instead of the ungated mirror.
    [string]$ModelRepo = '',
    [string]$HfToken = $env:HF_TOKEN,
    [string]$ModelId = '',
    [int]$ProxyPort = 8080,
    [switch]$SkipFirewall,
    [double]$Budget = 250.0
)

$ErrorActionPreference = 'Stop'
if (-not $ApiKey) {
    throw 'Set $env:LAMBDA_API_KEY (https://cloud.lambda.com/api_keys) or pass -ApiKey. Note Lambda accepts credit cards only, and the card must be authorised in the Billing page first.'
}
$headers = @{ Authorization = "Bearer $ApiKey"; accept = 'application/json' }
$api = 'https://cloud.lambda.com/api/v1'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$bootstrapPath = Join-Path $PSScriptRoot 'bootstrap.sh'
$keyPath = Join-Path $env:USERPROFILE '.ssh\av2-lambda-orcasaq'
$statePath = Join-Path $root 'tmp\lambda-orcasaq.json'

function Invoke-Lambda {
    param([string]$Method = 'GET', [string]$Path, $Body)
    $req = @{ Method = $Method; Uri = "$api$Path"; Headers = $headers; TimeoutSec = 120 }
    if ($null -ne $Body) {
        $req.ContentType = 'application/json'
        $req.Body = ($Body | ConvertTo-Json -Depth 10)
    }
    try {
        Invoke-RestMethod @req
    } catch {
        $msg = $_.Exception.Message
        $detail = $_.ErrorDetails.Message
        if ($detail) {
            $err = $null
            try { $err = $detail | ConvertFrom-Json } catch { }
            if ($err -and $err.error) {
                $msg = "Lambda API $Method $Path -> $($err.error.code): $($err.error.message)"
                if ($err.error.suggestion) { $msg = "$msg [$($err.error.suggestion)]" }
            } else {
                $msg = "Lambda API $Method $Path failed: $detail"
            }
        }
        throw $msg
    }
}

function Get-InstanceTypeMap {
    (Invoke-Lambda -Path '/instance-types').data
}

function Resolve-InstanceType {
    param($Map, [string]$Preferred)
    $name = $Preferred
    $names = @($Map.PSObject.Properties.Name)
    if ($names -notcontains $name) {
        $cand = $Map.PSObject.Properties | Where-Object { $_.Value.instance_type.gpu_description -match 'A6000' } | Select-Object -First 1
        if (-not $cand) { throw "instance type '$Preferred' is gone and no A6000 type is offered - run -Action Types" }
        $name = $cand.Name
    }
    $it = $Map.$name
    [pscustomobject]@{
        Name        = $name
        Description = $it.instance_type.description
        Gpu         = $it.instance_type.gpu_description
        Vcpus       = $it.instance_type.specs.vcpus
        MemoryGiB   = $it.instance_type.specs.memory_gib
        StorageGiB  = $it.instance_type.specs.storage_gib
        Rate        = [double]$it.instance_type.price_cents_per_hour / 100.0
        Regions     = @($it.regions_with_capacity_available)
    }
}

function Show-Cost {
    param([double]$Rate, [string]$Label)
    # Invariant formatting: a French Windows locale would otherwise print $76,30
    # while every doc says $76.30.
    $f = [Globalization.CultureInfo]::InvariantCulture
    Write-Host ("  {0} @ `${1}/h  ->  70 h = `${2}  |  140 h = `${3}  |  240 h = `${4}   (budget `${5})" -f `
        $Label, $Rate.ToString('F2', $f), (70 * $Rate).ToString('F2', $f), (140 * $Rate).ToString('F2', $f),
        (240 * $Rate).ToString('F2', $f), $Budget.ToString('F0', $f)) -ForegroundColor DarkGray
}

function Get-TargetInstance {
    $all = @((Invoke-Lambda -Path '/instances').data)
    @($all | Where-Object { $_.name -eq $Name })
}

function Ensure-SshKey {
    $pubPath = "$keyPath.pub"
    if (-not (Test-Path -LiteralPath $keyPath) -or -not (Test-Path -LiteralPath $pubPath)) {
        Write-Host "generating ssh key $keyPath" -ForegroundColor DarkGray
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $keyPath) | Out-Null
        & ssh-keygen -q -t ed25519 -N '""' -C 'av2-lambda-orcasaq' -f $keyPath
        if (-not (Test-Path -LiteralPath $pubPath)) { throw "ssh-keygen did not create $pubPath" }
    }
    $pub = (Get-Content -LiteralPath $pubPath -Raw).Trim()
    $keys = @((Invoke-Lambda -Path '/ssh-keys').data)
    $match = $keys | Where-Object { $_.public_key.Trim() -eq $pub }
    if ($match) {
        Write-Host "ssh key reused: $($match[0].name)" -ForegroundColor DarkGray
        return $match[0].name
    }
    # Lambda requires exactly one key per launch and identifies it by name.
    $keyName = 'av2-lambda-orcasaq'
    if ($keys | Where-Object { $_.name -eq $keyName }) {
        $keyName = "av2-lambda-orcasaq-$((Get-Random -Minimum 1000 -Maximum 9999))"
    }
    $created = Invoke-Lambda -Method POST -Path '/ssh-keys' -Body @{ name = $keyName; public_key = $pub }
    Write-Host "ssh key uploaded: $($created.data.name)" -ForegroundColor DarkGray
    return $created.data.name
}

function Get-EdgeRules {
    @((Invoke-Lambda -Path '/firewall-rulesets/global').data.rules)
}

# Lambda's VMs have no security group of their own; inbound is filtered by the
# account's global ruleset at the network edge. PATCH replaces the whole rule
# list, so existing rules are carried over verbatim and only tcp/<ProxyPort> is
# rewritten to point at the operator CIDR.
function Ensure-EdgeRule {
    $current = Get-EdgeRules
    $wanted = @()
    foreach ($r in $current) {
        if ($r.protocol -eq 'icmp' -or $r.protocol -eq 'all' -or -not $r.port_range) {
            $wanted += @{ protocol = $r.protocol; source_network = $r.source_network; description = $r.description }
        } else {
            $wanted += @{
                protocol       = $r.protocol
                port_range     = @([int]$r.port_range[0], [int]$r.port_range[1])
                source_network = $r.source_network
                description    = $r.description
            }
        }
    }
    $rule = @{
        protocol       = 'tcp'
        port_range     = @($ProxyPort, $ProxyPort)
        source_network = $OperatorCidr
        description    = 'AV2 OrcaSAQ token-gated proxy (operator IP only)'
    }
    $replaced = $false
    for ($i = 0; $i -lt $wanted.Count; $i++) {
        $r = $wanted[$i]
        if ($r.protocol -ne 'tcp' -or -not $r.port_range) { continue }
        if ($r.port_range[0] -le $ProxyPort -and $r.port_range[1] -ge $ProxyPort) {
            $wanted[$i] = $rule
            $replaced = $true
        }
    }
    if (-not $replaced) { $wanted += $rule }
    Invoke-Lambda -Method PATCH -Path '/firewall-rulesets/global' -Body @{ rules = $wanted } | Out-Null
    Write-Host "edge firewall: tcp/$ProxyPort open to $OperatorCidr only" -ForegroundColor Green
}

function Show-EdgeRules {
    foreach ($r in (Get-EdgeRules)) {
        if ($r.port_range) { $ports = "$($r.port_range[0])-$($r.port_range[1])" } else { $ports = '-' }
        Write-Host ("  {0,-5} {1,-12} {2,-20} {3}" -f $r.protocol, $ports, $r.source_network, $r.description)
    }
}

# cloud-init user-data: bootstrap.sh is base64'd (no quoting hazards, no second
# hop) and a 0700 runner exports any HF overrides before handing over to it.
function New-UserData {
    $script = [IO.File]::ReadAllText($bootstrapPath).Replace("`r`n", "`n")
    $script = $script.Replace('__EDGE_CIDR__', $OperatorCidr)
    $b64Script = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))

    $exports = ''
    if ($ModelRepo) { $exports += "export MODEL_REPO='$ModelRepo'`n" }
    if ($HfToken) { $exports += "export HF_TOKEN='$HfToken'`n" }
    if ($ModelId) { $exports += "export MODEL_ID='$ModelId'`n" }
    if ($ProxyPort -ne 8080) { $exports += "export PROXY_PORT='$ProxyPort'`n" }
    $runner = "#!/bin/bash`nset -euo pipefail`n$exports exec bash /opt/av2/av2-bootstrap.sh`n"
    $b64Runner = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($runner))

    @"
#cloud-config
hostname: $($Name.ToLower())
write_files:
  - path: /opt/av2/av2-bootstrap.sh
    owner: root:root
    permissions: '0755'
    encoding: b64
    content: $b64Script
  - path: /opt/av2/run-bootstrap.sh
    owner: root:root
    permissions: '0700'
    encoding: b64
    content: $b64Runner
runcmd:
  - [ bash, /opt/av2/run-bootstrap.sh ]
"@
}

function Connect-Ssh {
    param([string]$Ip, [string]$RemoteCommand)
    & ssh -i $keyPath -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ConnectTimeout=15 "ubuntu@$Ip" $RemoteCommand
}

switch ($Action) {
    'Types' {
        $map = Get-InstanceTypeMap
        $rows = foreach ($p in $map.PSObject.Properties) {
            [pscustomobject]@{
                type     = $p.Name
                gpu      = $p.Value.instance_type.gpu_description
                usd_h    = [double]$p.Value.instance_type.price_cents_per_hour / 100.0
                vcpus    = $p.Value.instance_type.specs.vcpus
                ram_GiB  = $p.Value.instance_type.specs.memory_gib
                disk_GiB = $p.Value.instance_type.specs.storage_gib
                regions  = (@($p.Value.regions_with_capacity_available).name -join ',')
            }
        }
        $one = @($rows | Where-Object { $_.type -like 'gpu_1x_*' } | Sort-Object usd_h)
        $one | Format-Table -AutoSize | Out-String -Width 220 | Write-Host
        $pick = $one | Where-Object { $_.type -eq $InstanceType } | Select-Object -First 1
        if (-not $pick) { $pick = $one | Where-Object { $_.gpu -match 'A6000' } | Select-Object -First 1 }
        if ($pick) {
            Write-Host "pick: $($pick.type) - $($pick.gpu) @ `$$($pick.usd_h)/h" -ForegroundColor Cyan
            Show-Cost -Rate $pick.usd_h -Label $pick.type
            if (-not $pick.regions) {
                Write-Host '  NO region currently lists capacity for this type - wait and re-run' -ForegroundColor Yellow
            } else {
                Write-Host "  capacity right now: $($pick.regions)" -ForegroundColor Green
            }
        }
    }

    'List' {
        $all = @((Invoke-Lambda -Path '/instances').data)
        Write-Host "instances: $($all.Count)" -ForegroundColor Cyan
        foreach ($t in $all) {
            Write-Host ("  {0} | {1} | {2} | {3} | {4}" -f $t.name, $t.status, $t.ip, $t.instance_type.name, $t.region.name)
        }
    }

    'Status' {
        $target = Get-TargetInstance
        if (-not $target) { Write-Host "no instance named '$Name'" -ForegroundColor Yellow; return }
        foreach ($t in $target) {
            Write-Host ("id={0} status={1} ip={2} region={3} type={4}" -f `
                $t.id, $t.status, $t.ip, $t.region.name, $t.instance_type.name) -ForegroundColor Cyan
            if ($t.first_healthy) {
                $since = [datetimeoffset]::Parse($t.first_healthy)
                $hours = ((Get-Date).ToUniversalTime() - $since.UtcDateTime).TotalHours
                $rate = 1.09
                if ($t.instance_type.price_cents_per_hour) { $rate = [double]$t.instance_type.price_cents_per_hour / 100.0 }
                Write-Host ("  healthy {0}h -> about `${1} so far at `${2}/h" -f `
                    $hours.ToString('F1', [Globalization.CultureInfo]::InvariantCulture),
                    ($hours * $rate).ToString('F2', [Globalization.CultureInfo]::InvariantCulture),
                    $rate.ToString('F2', [Globalization.CultureInfo]::InvariantCulture)) -ForegroundColor DarkGray
            }
            if ($t.ip) {
                Write-Host "  probe: curl.exe -s -H 'Authorization: Bearer <token>' http://$($t.ip):$ProxyPort/v1/models" -ForegroundColor DarkGray
            }
        }
    }

    'Create' {
        if (Get-TargetInstance) { throw "an instance named '$Name' already exists - run -Action Status" }
        if (-not (Test-Path -LiteralPath $bootstrapPath)) { throw "missing $bootstrapPath" }

        $it = Resolve-InstanceType -Map (Get-InstanceTypeMap) -Preferred $InstanceType
        Write-Host ("instance type: {0} - {1} / {2} vCPU / {3} GiB RAM / {4} GiB SSD" -f `
            $it.Name, $it.Gpu, $it.Vcpus, $it.MemoryGiB, $it.StorageGiB) -ForegroundColor Cyan
        Show-Cost -Rate $it.Rate -Label $it.Name

        $regions = @($it.Regions)
        if ($regions.Count -eq 0) {
            throw "no region reports capacity for $($it.Name) right now - watch with -Action Types and retry"
        }
        $chosen = $null
        if ($Region -ne 'auto') { $chosen = $regions | Where-Object { $_.name -eq $Region } | Select-Object -First 1 }
        if (-not $chosen) {
            if ($Region -ne 'auto') { Write-Host "-Region $Region has no capacity; using the first region that does" -ForegroundColor Yellow }
            $chosen = $regions[0]
        }
        Write-Host "region: $($chosen.name) - $($chosen.description)" -ForegroundColor Cyan

        $keyName = Ensure-SshKey
        if ($SkipFirewall) {
            Write-Host 'edge firewall untouched (-SkipFirewall): tcp/8080 will be open to the world' -ForegroundColor Yellow
        } else {
            Ensure-EdgeRule
        }

        $userData = New-UserData
        $body = @{
            region_name        = $chosen.name
            instance_type_name = $it.Name
            ssh_key_names      = @($keyName)
            name               = $Name
            user_data          = $userData
        }
        Write-Host 'launching (Lambda rate-limits this endpoint to one request per 12 s)...' -ForegroundColor Cyan
        $resp = Invoke-Lambda -Method POST -Path '/instance-operations/launch' -Body $body
        $id = @($resp.data.instance_ids)[0]
        @{
            id      = $id
            name    = $Name
            region  = $chosen.name
            type    = $it.Name
            rate    = $it.Rate
            created = (Get-Date).ToUniversalTime().ToString('o')
        } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
        Write-Host "launched $id" -ForegroundColor Green
        Write-Host 'first boot: driver check + ollama + 15.7 GB GGUF + nginx token gate (~8 min, no SSH needed)' -ForegroundColor DarkGray
        Write-Host 'next: .\provision_orcasaq.ps1 -Action Wait' -ForegroundColor Green
    }

    'Wait' {
        $target = Get-TargetInstance | Select-Object -First 1
        if (-not $target) { throw "no instance named '$Name' - run -Action Create" }
        $deadline = (Get-Date).AddMinutes(20)
        do {
            Start-Sleep -Seconds 15
            $cur = (Invoke-Lambda -Path "/instances/$($target.id)").data
            Write-Host ("  {0} {1}" -f $cur.status, $cur.ip) -ForegroundColor DarkGray
        } until ((($cur.status -eq 'active') -and $cur.ip) -or ((Get-Date) -gt $deadline))
        if (-not $cur.ip) { throw "instance $($target.id) never got a public IP (status=$($cur.status))" }
        if ($cur.status -ne 'active') { Write-Host "status is '$($cur.status)' - see the Lambda console" -ForegroundColor Yellow }

        Write-Host "public ip: $($cur.ip) - waiting for /opt/av2/READY (the 15.7 GB pull dominates)" -ForegroundColor Cyan
        $ready = ''
        $deadline = (Get-Date).AddMinutes(35)
        do {
            Start-Sleep -Seconds 20
            $ready = (@(Connect-Ssh -Ip $cur.ip -RemoteCommand 'sudo cat /opt/av2/READY 2>/dev/null || true') -join "`n").Trim()
            Write-Host ("  ready={0}" -f $(if ($ready) { 'partial' } else { 'no' })) -ForegroundColor DarkGray
        } until (($ready -match 'api_token=') -or ((Get-Date) -gt $deadline))
        if (-not ($ready -match 'api_token=')) {
            Write-Host 'bootstrap has not finished - inspect: .\provision_orcasaq.ps1 -Action Logs' -ForegroundColor Yellow
            return
        }
        Write-Host ''
        $ready | Write-Host
        Write-Host ''
        $token = ''
        if ($ready -match 'api_token=([0-9a-f]+)') { $token = $Matches[1] }
        Write-Host "base_url = http://$($cur.ip):$ProxyPort/v1" -ForegroundColor Green
        Write-Host "api_key  = $token" -ForegroundColor Green
        Write-Host ''
        Write-Host 'wire AV2 (back up .env, then restart the backend):' -ForegroundColor Cyan
        Write-Host "  cd tools\glm53; .\connect_av2.ps1 -BaseUrl 'http://$($cur.ip):$ProxyPort/v1' -ApiKey '$token' -Model 'orcasaq-cyber:27b' -Apply" -ForegroundColor DarkGray
    }

    'Logs' {
        $target = Get-TargetInstance | Select-Object -First 1
        if (-not $target) { throw "no instance named '$Name'" }
        if (-not $target.ip) { throw "instance $($target.id) has no IP yet (status=$($target.status))" }
        Connect-Ssh -Ip $target.ip -RemoteCommand 'sudo tail -n 40 /var/log/av2-bootstrap.log; echo ---READY---; sudo cat /opt/av2/READY 2>/dev/null || echo "not ready yet"'
        return
    }

    'Firewall' {
        if ($SkipFirewall) { throw '-SkipFirewall and -Action Firewall contradict each other' }
        Ensure-EdgeRule
        Show-EdgeRules
    }


    'Destroy' {
        $target = Get-TargetInstance
        if (-not $target) { Write-Host 'nothing to destroy' -ForegroundColor Yellow; return }
        $ids = @($target | ForEach-Object { $_.id })
        Invoke-Lambda -Method POST -Path '/instance-operations/terminate' -Body @{ instance_ids = $ids } | Out-Null
        Write-Host "terminate requested for $($ids -join ', ')" -ForegroundColor Green
        $deadline = (Get-Date).AddMinutes(5)
        do {
            Start-Sleep -Seconds 10
            $left = @(Get-TargetInstance)
            Write-Host ("  {0}" -f (@($left | ForEach-Object { "$($_.id)=$($_.status)" }) -join ' ')) -ForegroundColor DarkGray
            $busy = @($left | Where-Object { @('terminated', 'terminating') -notcontains $_.status })
        } until (($busy.Count -eq 0) -or ((Get-Date) -gt $deadline))
        if ($busy.Count -gt 0) {
            Write-Host 'still shutting down - billing stops when status=terminated; re-run -Action Status' -ForegroundColor Yellow
        } else {
            Write-Host 'billing stopped' -ForegroundColor Green
        }
        if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
        Write-Host '.env still points at the dead IP until connect_av2.ps1 is re-run' -ForegroundColor DarkYellow
    }
}

