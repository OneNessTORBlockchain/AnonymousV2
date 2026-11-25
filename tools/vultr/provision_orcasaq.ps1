# Anonymous V2 - Vultr Cloud GPU provisioning for OrcaSAQ-2-Cyber-27B-Uncensored.
#
#   $env:VULTR_API_KEY = '<personal access token>'
#   .\provision_orcasaq.ps1 -Action Create -OperatorCidr 173.177.72.31/32
#   .\provision_orcasaq.ps1 -Action Wait
#   .\provision_orcasaq.ps1 -Action Status
#   .\provision_orcasaq.ps1 -Action Destroy          # stops all billing
#   .\provision_orcasaq.ps1 -Action Plans            # READ-ONLY: balance + every vcg plan this key can see
#
# The 16 GB vcg-a16-* plan ($0.47/h) does NOT fit this model: 14.60 GiB of weights leave no
# room for KV cache inside 16 GB, and the ~200 GB/s slice caps decode near 13 tok/s. See
# docs/ORCASAQ-VULTR.md section 1. -Action Plans compares the catalogue before you spend.
#
# The 48 GB L40S is the only Vultr GPU class that leaves budget on the table:
# $1.671/h means the $250 credit covers ~149 running hours, so the 70 h
# (14 d x 5 h) pattern costs ~$117 while a 10-day continuous run (~240 h) would
# cost ~$401 and is NOT affordable.
#
# Everything the instance needs is baked into first boot: tools/vultr/
# bootstrap.sh is base64'd into cloud-init user-data, so the box converges
# without an interactive session and quoting can never break the script.
[CmdletBinding()]
param(
    [ValidateSet('Create', 'Status', 'Wait', 'Destroy', 'List', 'Logs', 'Plans')]
    [string]$Action = 'Status',
    [string]$Region = 'atl',
    [string]$Plan = 'vcg-l40s-16c-180g-48vram',
    [int]$OsId = 2284,
    [string]$OperatorCidr = '173.177.72.31/32',
    [string]$Label = 'av2-orcasaq',
    [string]$ApiKey = $env:VULTR_API_KEY,
    [double]$Credit = 250.0,
    [double]$Rate = 0            # $/h for the cost lines; 0 = look it up from GET /v2/plans
)

$ErrorActionPreference = 'Stop'
if (-not $ApiKey) { throw 'Set $env:VULTR_API_KEY (Vultr personal access token) or pass -ApiKey.' }
$headers = @{ Authorization = "Bearer $ApiKey" }
$api = 'https://api.vultr.com/v2'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$bootstrapPath = Join-Path $PSScriptRoot 'bootstrap.sh'
$statePath = Join-Path $root 'tmp\vultr-orcasaq.json'
function Get-PlanCatalog {
    # Paginated GET /v2/plans. Returns every plan of the given type visible to this API key.
    param([string]$Type = 'vcg')
    $all = @()
    $cursor = $null
    do {
        $path = "/plans?type=$Type&per_page=500"
        if ($cursor) { $path = "$path&cursor=$cursor" }
        $page = Invoke-Vultr -Path $path
        $all += @($page.plans)
        $cursor = $null
        if ($page.meta -and $page.meta.links -and $page.meta.links.next) {
            $next = [string]$page.meta.links.next
            $i = $next.IndexOf('cursor=')
            if ($i -ge 0) { $cursor = $next.Substring($i + 7) }
        }
    } while ($cursor)
    $all
}

function Get-PlanRate {
    # Hourly price straight from the API so cost lines stay honest for any -Plan.
    param([string]$PlanId)
    try {
        $hit = @(Get-PlanCatalog -Type 'vcg') | Where-Object { $_.id -eq $PlanId } | Select-Object -First 1
        if ($hit) { return [double]$hit.hourly_cost }
    } catch {
        Write-Host "plan lookup failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
    return 0.0
}

function Invoke-Vultr {
    param([string]$Method = 'GET', [string]$Path, $Body)
    $req = @{ Method = $Method; Uri = "$api$Path"; Headers = $headers; TimeoutSec = 120 }
    if ($null -ne $Body) {
        $req.ContentType = 'application/json'
        $req.Body = ($Body | ConvertTo-Json -Depth 8)
    }
    Invoke-RestMethod @req
}

function Write-CostLine {
    param([double]$Hours)
    $spend = $Hours * $Rate
    Write-Host ("  {0,6:N1} h x `${1:N3}/h = `${2,7:N2}   credit left: `${3,7:N2}" -f $Hours, $Rate, $spend, ($Credit - $spend)) -ForegroundColor DarkGray
}

function Get-TargetInstance {
    $list = Invoke-Vultr -Path '/instances'
    @($list.instances) | Where-Object { $_.label -eq $Label }
}

switch ($Action) {
    'Plans' {
        # READ-ONLY preflight: proves what this API key is actually allowed to see/deploy.
        $acct = (Invoke-Vultr -Path '/account').account
        Write-Host ("account: {0} <{1}>" -f $acct.name, $acct.email) -ForegroundColor Cyan
        Write-Host ("balance: `${0:N2}   pending charges: `${1:N2}" -f [double]$acct.balance, [double]$acct.pending_charges) -ForegroundColor DarkGray

        $catalog = @(Get-PlanCatalog -Type 'vcg')
        if ($catalog.Count -eq 0) {
            Write-Host 'no vcg plans visible to this API key - the account is still missing GPU / Deploy permission' -ForegroundColor Yellow
            return
        }
        Write-Host ("{0} GPU plan(s) visible; 'HERE' = sold in {1}:" -f $catalog.Count, $Region) -ForegroundColor Cyan
        foreach ($p in ($catalog | Sort-Object { [double]$_.hourly_cost })) {
            $here = @($p.locations) -contains $Region
            $r = [double]$p.hourly_cost
            $hrs = if ($r -gt 0) { $Credit / $r } else { 0 }
            Write-Host ("  {0,-28} {1,2}c {2,6} MB {3,3} GB VRAM  `${4:N3}/h  {5,5:N0} h credit  {6}  {7}" -f `
                    $p.id, $p.vcpu_count, $p.ram, $p.gpu_vram_gb, $r, $hrs,
                    $(if ($here) { 'HERE' } else { '    ' }), (@($p.locations) -join ',')) `
                -ForegroundColor $(if ($here) { 'Green' } else { 'DarkGray' })
        }
        $regions = @($catalog | ForEach-Object { $_.locations } | Sort-Object -Unique) -join ', '
        Write-Host "GPU regions on offer: $regions" -ForegroundColor DarkGray
    }
    'List' {
        $list = Invoke-Vultr -Path '/instances'
        $gpu = @($list.instances) | Where-Object { $_.plan -like 'vcg-*' }
        Write-Host "instances total: $(@($list.instances).Count) / GPU: $(@($gpu).Count)" -ForegroundColor Cyan
        $gpu | ForEach-Object { Write-Host ("  {0} | {1} | {2} | {3} | {4}" -f $_.id, $_.label, $_.status, $_.main_ip, $_.plan) }
    }
    'Status' {
        $target = Get-TargetInstance
        if (-not $target) { Write-Host "no instance labelled '$Label'" -ForegroundColor Yellow; return }
        $target | ForEach-Object {
            Write-Host ("id={0} status={1} ip={2} region={3} plan={4} power={5}" -f $_.id, $_.status, $_.main_ip, $_.region, $_.plan, $_.power_status) -ForegroundColor Cyan
        }
        if ($target.main_ip -and $target.main_ip -ne '0.0.0.0') {
            Write-Host "  probe: curl -H 'Authorization: Bearer <token>' http://$($target.main_ip):8080/v1/models" -ForegroundColor DarkGray
        }
    }

    'Create' {
        if (Get-TargetInstance) { throw "an instance labelled '$Label' already exists - run -Action Status" }
        if (-not (Test-Path -LiteralPath $bootstrapPath)) { throw "missing $bootstrapPath" }

        if ($Rate -le 0) {
            $Rate = Get-PlanRate -PlanId $Plan
            if ($Rate -le 0) {
                $Rate = 1.671
                Write-Host 'plan rate unavailable - cost lines assume $1.671/h (L40S 48 GB)' -ForegroundColor DarkYellow
            }
        }
        if ($Plan -like 'vcg-a16*') {
            Write-Host 'WARNING: 16 GB VRAM slice. The 15.68 GB GGUF needs NUM_CTX=4096 and PARALLEL=1 to' -ForegroundColor Yellow
            Write-Host '         fit; stock bootstrap.sh (NUM_CTX=32768 x PARALLEL=4) will spill to CPU at 2-4 tok/s.' -ForegroundColor Yellow
        }

        # ---- ssh key ------------------------------------------------------
        $pubPath = Join-Path $env:USERPROFILE '.ssh\av2-vultr-orcasaq.pub'
        if (-not (Test-Path -LiteralPath $pubPath)) { throw "missing $pubPath - generate it with ssh-keygen -t ed25519 -f $pubPath" }
        $pub = (Get-Content -LiteralPath $pubPath -Raw).Trim()
        $keys = Invoke-Vultr -Path '/ssh-keys'
        $existing = @($keys.ssh_keys) | Where-Object { $_.ssh_key.Trim() -eq $pub }
        if ($existing) {
            $keyId = $existing[0].id
            Write-Host "ssh key reused: $keyId" -ForegroundColor DarkGray
        } else {
            $created = Invoke-Vultr -Method POST -Path '/ssh-keys' -Body @{ name = 'av2-vultr-orcasaq'; ssh_key = $pub }
            $keyId = $created.ssh_key.id
            Write-Host "ssh key uploaded: $keyId" -ForegroundColor DarkGray
        }

        # ---- cloud-init (bootstrap.sh base64 - no quoting hazards) --------
        $script = [IO.File]::ReadAllText($bootstrapPath).Replace("`r`n", "`n")
        $script = $script.Replace('__OPERATOR_CIDR__', $OperatorCidr)
        $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
        $userData = @"
#cloud-config
hostname: av2-orcasaq
manage_etc_hosts: true
write_files:
  - path: /root/av2-bootstrap.sh
    owner: root:root
    permissions: '0755'
    encoding: b64
    content: $b64
runcmd:
  - [ bash, /root/av2-bootstrap.sh ]
"@
        $body = @{
            hostname = 'av2-orcasaq'; label = $Label; region = $Region; plan = $Plan
            os_id = $OsId; sshkey_id = @($keyId)
            user_data = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($userData))
            backups = 'disabled'; enable_ipv6 = $false; ddos_protection = $false
            activation_email = $false; tags = @('av2', 'orcasaq', 'llm-endpoint')
        }
        Write-Host "creating $Plan in $Region (os_id $OsId)..." -ForegroundColor Cyan
        try {
            $resp = Invoke-Vultr -Method POST -Path '/instances' -Body $body
        } catch {
            Write-Host "create failed: $($_.ErrorDetails.Message)" -ForegroundColor Red
            Write-Host 'GPU capacity moves between regions: retry with -Region ord|ewr|lax|mia|dfw|fra|ams' -ForegroundColor Yellow
            throw
        }
        $inst = $resp.instance
        @{ id = $inst.id; region = $Region; plan = $Plan; created = (Get-Date).ToUniversalTime().ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
        Write-Host "created $($inst.id) in $Region" -ForegroundColor Green
        Write-Host 'first boot: ollama + 15 GB pull + nginx token gate (~6 min, no SSH needed)' -ForegroundColor DarkGray
        Write-CostLine -Hours 70
        Write-CostLine -Hours 149
    }
    'Wait' {
        $target = Get-TargetInstance | Select-Object -First 1
        if (-not $target) { throw "no instance labelled '$Label'" }
        $deadline = (Get-Date).AddMinutes(25)
        do {
            Start-Sleep -Seconds 20
            $cur = (Invoke-Vultr -Path "/instances/$($target.id)").instance
            Write-Host ("  {0} {1}" -f $cur.status, $cur.main_ip) -ForegroundColor DarkGray
        } until (($cur.status -eq 'active' -and $cur.main_ip -ne '0.0.0.0') -or (Get-Date) -gt $deadline)
        if ($cur.main_ip -eq '0.0.0.0') { throw 'instance never received a public IP' }
        $ssh = "ssh -i `"$env:USERPROFILE\.ssh\av2-vultr-orcasaq`" -o StrictHostKeyChecking=no root@$($cur.main_ip)"
        Write-Host "active: $($cur.main_ip)" -ForegroundColor Green
        Write-Host 'then, once /opt/av2/READY exists:' -ForegroundColor Green
        Write-Host "  $ssh 'cat /opt/av2/READY'" -ForegroundColor DarkGray
    }
    'Logs' {
        $target = Get-TargetInstance | Select-Object -First 1
        if (-not $target) { throw "no instance labelled '$Label'" }
        $sshKey = Join-Path $env:USERPROFILE '.ssh\av2-vultr-orcasaq'
        ssh -i $sshKey -o StrictHostKeyChecking=no -o ConnectTimeout=20 "root@$($target.main_ip)" 'tail -n 40 /var/log/av2-bootstrap.log; echo ---READY---; cat /opt/av2/READY 2>/dev/null || echo "not ready yet"'
        return
    }
    'Destroy' {
        $target = Get-TargetInstance
        if (-not $target) { Write-Host 'nothing to destroy' -ForegroundColor Yellow; return }
        foreach ($item in $target) {
            Invoke-Vultr -Method DELETE -Path "/instances/$($item.id)" | Out-Null
            Write-Host "destroyed $($item.id) ($($item.label)) - billing stops now" -ForegroundColor Green
        }
        if (Test-Path -LiteralPath $statePath) { Remove-Item -LiteralPath $statePath -Force }
        Write-Host '.env still points at the dead IP until connect_av2.ps1 is re-run' -ForegroundColor DarkYellow
    }
}
