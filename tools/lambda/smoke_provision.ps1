# Offline checks for tools/lambda/provision_orcasaq.ps1 - no Lambda account needed.
# Loads only the function definitions from the script (the switch at the bottom is
# not executed) and swaps Invoke-Lambda for an in-memory fake.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$provisioner = Join-Path $root 'tools\lambda\provision_orcasaq.ps1'

$ast = [System.Management.Automation.Language.Parser]::ParseFile($provisioner, [ref]$null, [ref]$null)
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($f in $fns) { . ([scriptblock]::Create($f.Extent.Text)) }

# --- the variables the functions close over -----------------------------------
$Name = 'av2-orcasaq'
$OperatorCidr = '203.0.113.9/32'
$ProxyPort = 8080
$Budget = 250.0
$bootstrapPath = Join-Path $root 'tools\lambda\bootstrap.sh'
$keyPath = Join-Path $env:USERPROFILE '.ssh\av2-lambda-orcasaq'
$api = 'https://example.invalid/api/v1'
$headers = @{}
$statePath = Join-Path $PSScriptRoot 'lambda-state-test.json'

$script:calls = @()
function Invoke-Lambda {
    param([string]$Method = 'GET', [string]$Path, $Body)
    $script:calls += [pscustomobject]@{ Method = $Method; Path = $Path; Body = $Body }
    switch -Regex ($Path) {
        '^/firewall-rulesets/global$' {
            return [pscustomobject]@{ data = [pscustomobject]@{ id = 'global'; rules = @(
                [pscustomobject]@{ protocol = 'tcp'; port_range = @(22, 22); source_network = '0.0.0.0/0'; description = 'Allow SSH from anywhere' },
                [pscustomobject]@{ protocol = 'icmp'; port_range = $null; source_network = '0.0.0.0/0'; description = 'ICMP' }
            ) } }
        }
        '^/instances$' {
            return [pscustomobject]@{ data = @(
                [pscustomobject]@{ id = 'aaa'; name = 'other-box'; status = 'active'; ip = '198.51.100.1' },
                [pscustomobject]@{ id = 'bbb'; name = 'av2-orcasaq'; status = 'active'; ip = '198.51.100.2' }
            ) }
        }
        default { return [pscustomobject]@{ data = @() } }
    }
}

$fail = 0
function Check {
    param([string]$What, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { Write-Host "  PASS  $What" -ForegroundColor Green }
    else { Write-Host "  FAIL  $What $Detail" -ForegroundColor Red; $script:fail++ }
}

# --- 1. cloud-init user-data --------------------------------------------------
$ud = New-UserData
Check 'user-data is cloud-config' ($ud.StartsWith('#cloud-config'))
Check 'user-data has both write_files' (([regex]::Matches($ud, 'path: /opt/av2/')).Count -eq 2) $ud.Substring(0, 60)

$b64 = [regex]::Match($ud, "path: /opt/av2/av2-bootstrap\.sh[\s\S]*?content: (\S+)").Groups[1].Value
$decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
Check 'bootstrap round-trips' ($decoded -match 'OrcaSAQ-2-Cyber-27B bootstrap for a Lambda Cloud')
Check 'EDGE_CIDR substituted' ($decoded -match '203\.0\.113\.9/32') 'no substitution'
Check 'no placeholder left' ($decoded -notmatch '__EDGE_CIDR__')
Check 'LF line endings only' ($decoded -notmatch "`r")

$b64r = [regex]::Match($ud, "path: /opt/av2/run-bootstrap\.sh[\s\S]*?content: (\S+)").Groups[1].Value
$runner = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64r))
Check 'runner execs the bootstrap' ($runner -match 'exec bash /opt/av2/av2-bootstrap.sh')
Check 'runner exports nothing by default' ($runner -notmatch 'export ')

# --- 2. overrides go into the runner, not the bootstrap copy ------------------
$ModelRepo = 'orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF'
$HfToken = 'hf_test'
$ModelId = 'orcasaq-cyber:27b'
$ud2 = New-UserData
$b64r2 = [regex]::Match($ud2, "path: /opt/av2/run-bootstrap\.sh[\s\S]*?content: (\S+)").Groups[1].Value
$runner2 = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64r2))
Check 'runner exports MODEL_REPO' ($runner2 -match "export MODEL_REPO='orcarouter/OrcaSAQ-2-Cyber-27B-Uncensored-GGUF'")
Check 'runner exports HF_TOKEN' ($runner2 -match "export HF_TOKEN='hf_test'")
$b64s2 = [regex]::Match($ud2, "path: /opt/av2/av2-bootstrap\.sh[\s\S]*?content: (\S+)").Groups[1].Value
$script2 = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64s2))
Check 'HF token absent from the bootstrap copy' (-not $script2.Contains('hf_test'))
$ModelRepo = ''; $HfToken = ''; $ModelId = ''

# --- 3. edge firewall: preserve, replace, append ------------------------------
$script:calls = @()
Ensure-EdgeRule
$patch = @($script:calls | Where-Object { $_.Method -eq 'PATCH' })
Check 'one PATCH to the global ruleset' ($patch.Count -eq 1)
$rules = @($patch[0].Body.rules)
Check 'ssh rule preserved' (@($rules | Where-Object { $_.protocol -eq 'tcp' -and $_.port_range -and $_.port_range[0] -eq 22 -and $_.port_range[1] -eq 22 -and $_.source_network -eq '0.0.0.0/0' }).Count -eq 1)
Check 'icmp rule preserved without port_range' (@($rules | Where-Object { $_.protocol -eq 'icmp' -and -not $_.port_range }).Count -eq 1)
Check 'proxy rule restricted to the operator cidr' (@($rules | Where-Object { $_.protocol -eq 'tcp' -and $_.port_range -and $_.port_range[0] -eq 8080 -and $_.port_range[1] -eq 8080 -and $_.source_network -eq '203.0.113.9/32' }).Count -eq 1)

$script:calls = @()
Ensure-EdgeRule
$patch2 = @($script:calls | Where-Object { $_.Method -eq 'PATCH' })[0]
Check 'idempotent: still exactly one proxy rule' (@($patch2.Body.rules | Where-Object { $_.port_range -and $_.port_range[0] -eq 8080 }).Count -eq 1)

# --- 4. instance selection by name -------------------------------------------
$target = @(Get-TargetInstance)
Check 'target picked by name' ($target.Count -eq 1 -and $target[0].id -eq 'bbb')

# --- 5. instance type resolution --------------------------------------------
$map = [pscustomobject]@{
    gpu_1x_a6000 = [pscustomobject]@{
        instance_type = [pscustomobject]@{
            name = 'gpu_1x_a6000'; description = '1x NVIDIA RTX A6000'; gpu_description = 'RTX A6000 (48 GB PCIe)'
            price_cents_per_hour = 109; specs = [pscustomobject]@{ vcpus = 14; memory_gib = 100; storage_gib = 512; gpus = 1 }
        }
        regions_with_capacity_available = @(
            [pscustomobject]@{ name = 'us-west-1'; description = 'California, USA' },
            [pscustomobject]@{ name = 'europe-central-1'; description = 'Germany' }
        )
    }
    gpu_1x_a10 = [pscustomobject]@{
        instance_type = [pscustomobject]@{
            name = 'gpu_1x_a10'; description = '1x NVIDIA A10'; gpu_description = 'A10 (24 GB PCIe)'
            price_cents_per_hour = 129; specs = [pscustomobject]@{ vcpus = 30; memory_gib = 226; storage_gib = 1335; gpus = 1 }
        }
        regions_with_capacity_available = @()
    }
}
$it = Resolve-InstanceType -Map $map -Preferred 'gpu_1x_a6000'
Check 'a6000 rate parsed from cents' ($it.Rate -eq 1.09) "$($it.Rate)"
Check 'regions carried through' (@($it.Regions).Count -eq 2)
$it2 = Resolve-InstanceType -Map $map -Preferred 'gpu_9x_ghost'
Check 'unknown type falls back to an A6000' ($it2.Name -eq 'gpu_1x_a6000') "$($it2.Name)"

# --- 6. cost line renders without a $ interpolation bug ----------------------
$out = & { Show-Cost -Rate 1.09 -Label 'gpu_1x_a6000' } 6>&1
Check 'cost line mentions 76.30' ("$out" -match '76\.30') "$out"

Write-Host ''
if ($fail -eq 0) { Write-Host 'all offline checks passed' -ForegroundColor Green; exit 0 }
Write-Host "$fail check(s) failed" -ForegroundColor Red
exit 1

