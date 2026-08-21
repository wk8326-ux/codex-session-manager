[CmdletBinding()]
param(
    [string]$Version = '14.0.0',
    [string]$Proxy = ''
)

$ErrorActionPreference = 'Stop'
$packageId = 'Flameshot.Flameshot'

function Find-Flameshot {
    $candidates = @(
        $env:CSM_FLAMESHOT_PATH,
        $env:LPC_FLAMESHOT_PATH,
        (Join-Path $env:ProgramFiles 'Flameshot\bin\flameshot.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Flameshot\bin\flameshot.exe')
    )
    foreach ($candidate in $candidates) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    $command = Get-Command flameshot.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) { return $command.Source }
    return ''
}

function Get-FlameshotVersion([string]$Executable) {
    try {
        $versionOutput = (& $Executable --version 2>$null | Select-Object -First 1)
        if ([string]$versionOutput -match 'Flameshot v(\d+\.\d+\.\d+)') {
            return $Matches[1]
        }
    }
    catch {}
    return ''
}

if ($env:OS -ne 'Windows_NT') {
    throw '[CSM] The Flameshot installer supports Windows only.'
}

$existing = Find-Flameshot
if (-not [string]::IsNullOrWhiteSpace($existing)) {
    $existingVersion = Get-FlameshotVersion $existing
    if ($existingVersion -eq $Version) {
        Write-Host "[CSM] Flameshot $Version is already installed: $existing"
        exit 0
    }
    Write-Host "[CSM] Flameshot version $existingVersion will be replaced with $Version."
}

$winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if ($null -eq $winget) {
    throw '[CSM] winget was not found. Install Windows App Installer first.'
}

$arguments = @(
    'install',
    '--id', $packageId,
    '--exact',
    '--version', $Version,
    '--silent',
    '--accept-package-agreements',
    '--accept-source-agreements',
    '--disable-interactivity'
)
if (-not [string]::IsNullOrWhiteSpace($Proxy)) {
    $proxyUri = $null
    if (
        -not [Uri]::TryCreate($Proxy, [UriKind]::Absolute, [ref]$proxyUri) -or
        $proxyUri.Scheme -notin @('http', 'https')
    ) {
        throw '[CSM] Proxy must be a valid HTTP or HTTPS URL.'
    }
    & $winget.Source settings --enable ProxyCommandLineOptions | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw '[CSM] Could not enable winget command-line proxy support.'
    }
    $arguments += @('--proxy', $Proxy)
}

Write-Host "[CSM] Installing open-source screenshot tool Flameshot $Version..."
& $winget.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "[CSM] Flameshot installation failed with winget exit code $LASTEXITCODE."
}

$installed = Find-Flameshot
if ([string]::IsNullOrWhiteSpace($installed)) {
    throw '[CSM] winget finished but flameshot.exe was not found.'
}
$installedVersion = Get-FlameshotVersion $installed
if ($installedVersion -ne $Version) {
    throw "[CSM] Expected Flameshot $Version but found $installedVersion."
}
Write-Host "[CSM] Flameshot $installedVersion installed: $installed"
