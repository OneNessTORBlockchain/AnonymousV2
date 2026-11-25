# ovh_mint_ck.ps1
# Mints a fresh OVHcloud consumer key for the Application key in OVH_APP_KEY,
# scoped to Public Cloud project management. Prints a validation URL; the key
# only becomes usable after you approve it in a browser and log in to OVH.
#
# Usage:
#   $env:OVH_APP_KEY    = '...'
#   $env:OVH_APP_SECRET = '...'
#   .\tools\glm53\ovh_mint_ck.ps1
#
# Exit codes: 0 = validation URL issued, 1 = failure.

[CmdletBinding()]
param(
    [string]$Endpoint,
    [string]$Redirection = 'https://ca.ovh.com/manager/'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ovh_api.ps1')

try {
    if (-not $env:OVH_APP_KEY) { throw 'Set $env:OVH_APP_KEY first.' }
    if (-not $env:OVH_APP_SECRET) { throw 'Set $env:OVH_APP_SECRET first.' }

    $rules = @(
        @{ method = 'GET';    path = '/me' },
        # An account with no Public Cloud project can only get one through the
        # order flow, and a checked-out order still has to be accepted and paid
        # (even at $0 CAD). Without these three rules the API can create the
        # order but can never look at it again, so the project never appears.
        @{ method = 'GET';    path = '/me/order' },
        @{ method = 'GET';    path = '/me/order/*' },
        @{ method = 'POST';   path = '/me/order/*' },
        # Read-only, and the only way to tell "no card on file" (which makes every
        # checkout answer "You are not able to order") from "an order is unpaid".
        @{ method = 'GET';    path = '/me/paymentMean/*' },
        @{ method = 'GET';    path = '/cloud/project' },
        @{ method = 'GET';    path = '/cloud/project/*' },
        @{ method = 'POST';   path = '/cloud/project/*' },
        @{ method = 'PUT';    path = '/cloud/project/*' },
        @{ method = 'DELETE'; path = '/cloud/project/*' },
        @{ method = 'GET';    path = '/order/cart' },
        @{ method = 'POST';   path = '/order/cart' },
        @{ method = 'GET';    path = '/order/cart/*' },
        @{ method = 'POST';   path = '/order/cart/*' }
    )

    Write-Host "Endpoint     : $(Get-OvhEndpoint $Endpoint)"
    Write-Host "Application  : $env:OVH_APP_KEY"
    Write-Host ''

    $cred = Request-OvhConsumerKey -AccessRules $rules -Redirection $Redirection -Endpoint $Endpoint

    Write-Host "Consumer key : $($cred.consumerKey)"
    Write-Host "State        : $($cred.state)"
    Write-Host ''
    Write-Host 'NEXT STEP: open this URL, log in to OVH, and approve the rights:'
    Write-Host ''
    Write-Host "  $($cred.validationUrl)"
    Write-Host ''
    Write-Host ('Then: $env:OVH_CONSUMER_KEY = {0}' -f $cred.consumerKey)
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
