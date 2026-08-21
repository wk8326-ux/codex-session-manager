[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$Destination = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $projectRoot 'desktop\src-tauri\resources\backend'
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
$buildRoot = Join-Path $projectRoot 'build\pyinstaller'
$distRoot = Join-Path $projectRoot 'build\python-runtime'
$specPath = Join-Path $projectRoot 'packaging\lpc-service.spec'

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --workpath $buildRoot `
    --distpath $distRoot `
    $specPath
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller failed to build lpc-service.'
}

$source = Join-Path $distRoot 'lpc-service'
if (-not (Test-Path -LiteralPath (Join-Path $source 'lpc-service.exe') -PathType Leaf)) {
    throw "Packaged backend was not found: $source"
}
if (Test-Path -LiteralPath $Destination) {
    Remove-Item -LiteralPath $Destination -Recurse -Force
}
New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $Destination -Recurse
Write-Host "[LPC] Python runtime ready at $Destination"
