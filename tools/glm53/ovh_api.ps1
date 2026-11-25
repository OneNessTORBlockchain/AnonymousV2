# ovh_api.ps1
# Minimal OVHcloud API v1 client: request signing, credential minting, JSON calls.
# No SDK required. Dot-source this file, then call Invoke-OvhApi / Request-OvhConsumerKey.
#
# Required environment variables:
#   OVH_APP_KEY        application key
#   OVH_APP_SECRET     application secret
#   OVH_CONSUMER_KEY   consumer key (not needed for Request-OvhConsumerKey)
#   OVH_ENDPOINT       optional: https://ca.api.ovh.com/1.0 (default) | .../eu | .../us
#
# Why hand-rolled signing: OVHcloud API v1 uses a SHA1 signature over
#   secret+CK+method+url+body+timestamp
# so every call needs a fresh server timestamp. Bearer tokens are API v2 only and
# are a different auth system entirely, so they cannot be mixed with v1 here.

$ErrorActionPreference = 'Stop'

function Get-OvhEndpoint {
    [CmdletBinding()]
    param([string]$Endpoint)
    if ($Endpoint) { return $Endpoint.TrimEnd('/') }
    if ($env:OVH_ENDPOINT) { return $env:OVH_ENDPOINT.TrimEnd('/') }
    return 'https://ca.api.ovh.com/1.0'
}

function Get-OvhTime {
    [CmdletBinding()]
    param([string]$Endpoint)
    $ep = Get-OvhEndpoint $Endpoint
    $r = Invoke-OvhRawRequest -Method 'GET' -Url ($ep + '/auth/time') -Headers @{}
    if ($r.Status -ne 200) {
        throw "GET /auth/time failed: HTTP $($r.Status) $($r.Body)"
    }
    return $r.Body.Trim()
}

function Get-OvhSignature {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$AppSecret,
        [Parameter(Mandatory)][AllowEmptyString()][string]$ConsumerKey,
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Body,
        [Parameter(Mandatory)][string]$Timestamp
    )
    $raw = $AppSecret + '+' + $ConsumerKey + '+' + $Method + '+' + $Url + '+' + $Body + '+' + $Timestamp
    $sha = [System.Security.Cryptography.SHA1]::Create()
    try {
        $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($raw))
    } finally {
        $sha.Dispose()
    }
    return '$1$' + (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Invoke-OvhRawRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [hashtable]$Headers = @{},
        [AllowEmptyString()][string]$Body = ''
    )
    $req = [System.Net.WebRequest]::Create($Url)
    try {
        $req.Method = $Method
        $req.Timeout = 60000
        $req.Accept = 'application/json'
        foreach ($k in $Headers.Keys) { $req.Headers.Add($k, [string]$Headers[$k]) }
        if ($Body) {
            $bytes = [System.Text.Encoding]::UTF8.GetBytes($Body)
            $req.ContentType = 'application/json'
            $req.ContentLength = $bytes.Length
            $s = $req.GetRequestStream()
            try { $s.Write($bytes, 0, $bytes.Length) } finally { $s.Close() }
        }
        $resp = $req.GetResponse()
        $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
        try { $text = $sr.ReadToEnd() } finally { $sr.Close() }
        $code = [int]$resp.StatusCode
        $resp.Close()
        return @{ Status = $code; Body = $text }
    } catch [System.Net.WebException] {
        $er = $_.Exception.Response
        if ($er) {
            $sr = New-Object System.IO.StreamReader($er.GetResponseStream())
            try { $text = $sr.ReadToEnd() } finally { $sr.Close() }
            $code = [int]$er.StatusCode
            return @{ Status = $code; Body = $text }
        }
        return @{ Status = -1; Body = $_.Exception.Message }
    }
}

function Invoke-OvhApi {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PUT', 'DELETE')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        $Body = $null,
        [switch]$Anonymous,
        [string]$Endpoint
    )
    $ep = Get-OvhEndpoint $Endpoint
    $url = $ep + $Path
    $json = ''
    if ($null -ne $Body) {
        if ($Body -is [string]) { $json = $Body } else { $json = ($Body | ConvertTo-Json -Depth 10 -Compress) }
    }
    if (-not $env:OVH_APP_KEY) { throw 'OVH_APP_KEY is not set.' }
    if (-not $env:OVH_APP_SECRET) { throw 'OVH_APP_SECRET is not set.' }
    $ts = Get-OvhTime -Endpoint $ep
    $headers = @{
        'X-Ovh-Application' = $env:OVH_APP_KEY
        'X-Ovh-Timestamp'   = $ts
        'Accept-Language'   = 'en-US'
    }
    if ($Anonymous) {
        $headers['X-Ovh-Signature'] = Get-OvhSignature -AppSecret $env:OVH_APP_SECRET `
            -ConsumerKey '' -Method $Method -Url $url -Body $json -Timestamp $ts
    } else {
        if (-not $env:OVH_CONSUMER_KEY) { throw 'OVH_CONSUMER_KEY is not set.' }
        $headers['X-Ovh-Consumer'] = $env:OVH_CONSUMER_KEY
        $headers['X-Ovh-Signature'] = Get-OvhSignature -AppSecret $env:OVH_APP_SECRET `
            -ConsumerKey $env:OVH_CONSUMER_KEY -Method $Method -Url $url -Body $json -Timestamp $ts
    }
    $r = Invoke-OvhRawRequest -Method $Method -Url $url -Headers $headers -Body $json
    $parsed = $null
    if ($r.Body) {
        try { $parsed = $r.Body | ConvertFrom-Json } catch { $parsed = $null }
    }
    $errCode = $null
    if ($parsed -and ($parsed.PSObject.Properties.Name -contains 'errorCode')) { $errCode = $parsed.errorCode }
    $msg = $null
    if ($parsed -and ($parsed.PSObject.Properties.Name -contains 'message')) { $msg = $parsed.message }
    return @{
        Status    = $r.Status
        Body      = $r.Body
        Json      = $parsed
        ErrorCode = $errCode
        Message   = $msg
        Ok        = ($r.Status -ge 200 -and $r.Status -lt 300)
    }
}

function Request-OvhConsumerKey {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object[]]$AccessRules,
        [string]$Redirection = 'https://ca.ovh.com/manager/',
        [string]$Endpoint
    )
    $normalized = New-Object System.Collections.ArrayList
    foreach ($rule in $AccessRules) {
        [void]$normalized.Add([ordered]@{ method = $rule.method; path = $rule.path })
    }
    $payload = [ordered]@{ accessRules = $normalized.ToArray(); redirection = $Redirection }
    $r = Invoke-OvhApi -Method 'POST' -Path '/auth/credential' -Body $payload -Anonymous -Endpoint $Endpoint
    if (-not $r.Ok) {
        throw "POST /auth/credential failed: HTTP $($r.Status) $($r.Body)"
    }
    return $r.Json
}

# StrictMode-safe property read: OVH omits fields that are not set (available,
# quota, ipAddresses) instead of sending nulls, and bare $obj.field would throw.
function Get-Field {
    param($Obj, [string]$Name, $Default = $null)
    if ($null -eq $Obj) { return $Default }
    $p = $Obj.PSObject.Properties[$Name]
    if ($p -and $null -ne $p.Value) { return $p.Value }
    return $Default
}

function Get-OvhCloudProjects {
    [CmdletBinding()]
    param([string]$Endpoint)
    $r = Invoke-OvhApi -Method 'GET' -Path '/cloud/project' -Endpoint $Endpoint
    if (-not $r.Ok) {
        if ($r.Message -eq 'This credential is not valid') {
            throw ('The consumer key is not approved yet (OVH says "This credential is not valid"). ' +
                   'Run .\tools\glm53\ovh_mint_ck.ps1, open the validationUrl it prints, log in, and approve.')
        }
        if ($r.ErrorCode -eq 'NOT_GRANTED_CALL') {
            throw ('The consumer key is approved but lacks GET /cloud/project. Mint a key with the ' +
                   'cloud-project rights: .\tools\glm53\ovh_mint_ck.ps1')
        }
        throw "GET /cloud/project failed: HTTP $($r.Status) $($r.Body)"
    }
    return $r.Json
}

function Test-OvhAccess {
    [CmdletBinding()]
    param([string]$Endpoint)
    $r = Invoke-OvhApi -Method 'GET' -Path '/me' -Endpoint $Endpoint
    if ($r.Ok) { return $true }
    if ($r.ErrorCode -eq 'NOT_GRANTED_CALL') {
        throw ('Consumer key authenticates but has no rights on GET /me. Mint a new key: ' +
               '.\tools\glm53\ovh_mint_ck.ps1')
    }
    throw "GET /me failed: HTTP $($r.Status) $($r.Body)"
}
