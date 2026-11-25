# Anonymous V2 - stop paying for the GLM-5.3 GPU VM.
#
#   .\azure-teardown.ps1                          # dry run, prints what would happen
#   .\azure-teardown.ps1 -Execute                 # deallocate: disks and IP kept, GPU billing stops
#   .\azure-teardown.ps1 -Execute -DeleteAll      # delete VM+NIC+IP+NSG+VNet, keep the OS disk
#   .\azure-teardown.ps1 -Execute -DeleteAll -DeleteDisk   # everything gone
#
# Deallocate is the default action because it is the reversible one: the 72 GiB of
# weights stay on the OS disk, so the next start is a plain boot instead of a full
# re-download.  It still costs disk (~10-20 USD/month for 128 GiB), which is the
# number people forget.
[CmdletBinding()]
param(
    [string]$ResourceGroup = 'anon',
    [string]$VmName = 'av2-glm53',
    [switch]$DeleteAll,
    [switch]$DeleteDisk,
    [switch]$Execute
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Write-Note([string]$Text) { Write-Host "   $Text" -ForegroundColor DarkGray }
function Write-Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }

function Invoke-Or-Print {
    param([Parameter(Mandatory)][string[]]$Arguments, [switch]$TolerateFailure)
    Write-Host ('   $ az ' + ($Arguments -join ' ')) -ForegroundColor DarkGray
    if (-not $Execute) { return }
    & az @Arguments 2>&1 | Out-String | ForEach-Object { if ($_.Trim()) { Write-Note $_.Trim() } }
    if ($LASTEXITCODE -ne 0) {
        if ($TolerateFailure) { Write-Warn 'ignored (already gone?)' } else { throw "az $($Arguments -join ' ') failed" }
    }
}

Write-Host 'AV2 GLM-5.3 teardown' -ForegroundColor Green
Write-Host ("  target : {0}/{1}" -f $ResourceGroup, $VmName)
Write-Host ("  mode   : {0}" -f $(if ($Execute) { 'EXECUTE' } else { 'dry run' }))

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Warn 'az CLI not found on PATH; nothing can be done from here.'
    Write-Warn 'Fallback: portal.azure.com -> resource group -> VM -> Stop (deallocates).'
    exit 2
}

Write-Step 'Confirm the VM exists'
$state = $null
try {
    $state = (& az vm show -d -g $ResourceGroup -n $VmName --query powerState -o tsv 2>&1 | Out-String).Trim()
} catch { $state = $null }
if ($LASTEXITCODE -ne 0) {
    Write-Warn "VM $VmName not found in $ResourceGroup (or not signed in)."
    Write-Note 'Nothing to do.  Check with: az vm list -g ' + $ResourceGroup + ' -o table'
    exit 1
}
Write-Note "current power state: $state"
if ($state -eq 'VM deallocated') {
    Write-Note 'already deallocated: GPU/compute billing is stopped; only the disk still bills.'
}

Write-Step 'Plan'
if (-not $DeleteAll) {
    Invoke-Or-Print @('vm', 'deallocate', '-g', $ResourceGroup, '-n', $VmName)
    Write-Note 'the OS disk and static IP are kept, so wake_proxy.py can start this VM again'
    Write-Note 'static public IP kept: a Standard IP that stays allocated bills a small hourly rate'
}
else {
    Invoke-Or-Print @('vm', 'delete', '--yes', '-g', $ResourceGroup, '-n', $VmName) -TolerateFailure
    foreach ($resource in @("$VmName-nic", "$VmName-ip", "$VmName-nsg", "$VmName-vnet")) {
        $type = switch -Regex ($resource) {
            '-nic$' { 'nic' }
            '-ip$'  { 'public-ip' }
            '-nsg$' { 'nsg' }
            default { 'vnet' }
        }
        Invoke-Or-Print @('network', $type, 'delete', '-g', $ResourceGroup, '-n', $resource, '--yes') -TolerateFailure
    }
    Write-Note 'the OS disk survives a VM delete because the deploy tags it --os-disk-delete-option Delete'
    Write-Note '...unless the delete honoured that flag, in which case the weights are gone too'
    if ($DeleteDisk) {
        Invoke-Or-Print @('disk', 'list', '-g', $ResourceGroup, '--query', "[?contains(name,'$VmName')].name", '-o', 'tsv')
        Write-Note 'delete any disk listed above with:'
        Write-Note "  az disk delete -g $ResourceGroup -n <disk-name> --yes"
    }
}

Write-Step 'After'
if (-not $Execute) {
    Write-Host '   Dry run: nothing changed.' -ForegroundColor Yellow
    Write-Host '   -Execute performs exactly the commands above.' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '   Remember to stop tools/glm53/wake_proxy.py too: it will call az vm start' -ForegroundColor Gray
    Write-Host '   on the next request and undo a deallocate.' -ForegroundColor Gray
    exit 0
}

Write-Host '   Done.  Confirm in the portal that the VM reads "Stopped (deallocated)",' -ForegroundColor Green
Write-Host '   not "Stopped" -- a merely stopped VM still bills compute.' -ForegroundColor Green
Write-Host ''
Write-Host '   Stop the relay as well, or it will wake the VM on the next request.' -ForegroundColor Yellow
