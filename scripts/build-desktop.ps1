[CmdletBinding()]
param(
    [ValidateSet('Build', 'BackendOnly')]
    [string]$Action = 'Build',
    [switch]$SkipBackend
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$desktopRoot = Join-Path $projectRoot 'desktop'
$tauriRoot = Join-Path $desktopRoot 'src-tauri'
$resourceRoot = Join-Path $tauriRoot 'resources'
$artifactRoot = Join-Path $projectRoot 'release'

if (-not $SkipBackend) {
    & (Join-Path $PSScriptRoot 'build-python-runtime.ps1') `
        -Destination (Join-Path $resourceRoot 'backend')
}
Copy-Item `
    -LiteralPath (Join-Path $PSScriptRoot 'manage-system-startup.ps1') `
    -Destination (Join-Path $resourceRoot 'scripts\manage-system-startup.ps1') `
    -Force

if ($Action -eq 'BackendOnly') {
    exit 0
}

Push-Location $desktopRoot
try {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Tauri build failed.' }
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
$installer = Get-ChildItem `
    -LiteralPath (Join-Path $tauriRoot 'target\release\bundle\nsis') `
    -Filter '*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $installer) { throw 'NSIS installer was not produced.' }
$installerTarget = Join-Path $artifactRoot 'LocalProjectConsole-Setup-x64.exe'
Copy-Item -LiteralPath $installer.FullName -Destination $installerTarget -Force

$portableStage = Join-Path $projectRoot 'build\portable-package'
if (Test-Path -LiteralPath $portableStage) {
    Remove-Item -LiteralPath $portableStage -Recurse -Force
}
New-Item -ItemType Directory -Path $portableStage -Force | Out-Null
Copy-Item `
    -LiteralPath (Join-Path $tauriRoot 'target\release\LocalProjectConsole.exe') `
    -Destination $portableStage
Copy-Item -LiteralPath (Join-Path $resourceRoot 'backend') -Destination $portableStage -Recurse
Copy-Item -LiteralPath (Join-Path $resourceRoot 'scripts') -Destination $portableStage -Recurse
New-Item -ItemType File -Path (Join-Path $portableStage 'portable.flag') -Force | Out-Null

$portableTarget = Join-Path $artifactRoot 'LocalProjectConsole-Portable-x64.zip'
if (Test-Path -LiteralPath $portableTarget) {
    Remove-Item -LiteralPath $portableTarget -Force
}
Compress-Archive -Path (Join-Path $portableStage '*') -DestinationPath $portableTarget

foreach ($artifact in @($installerTarget, $portableTarget)) {
    $hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    $line = "$($hash.Hash.ToLowerInvariant())  $([System.IO.Path]::GetFileName($artifact))"
    Set-Content -LiteralPath "$artifact.sha256" -Value $line -Encoding ascii
}
Write-Host "[LPC] Release artifacts ready at $artifactRoot"
