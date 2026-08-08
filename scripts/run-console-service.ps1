[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$appPath = Join-Path $ProjectRoot 'app.py'
if (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) {
    throw "app.py was not found under $ProjectRoot"
}

$runtimeDirectory = Join-Path $ProjectRoot '.runtime\system-startup'
New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
$logPath = Join-Path $runtimeDirectory 'console-service.log'
$previousLogPath = Join-Path $runtimeDirectory 'console-service.previous.log'
if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 10MB) {
    Remove-Item -LiteralPath $previousLogPath -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $logPath -Destination $previousLogPath -Force
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
$arguments = @('-3', '-u', 'app.py')
if ($null -eq $launcher) {
    $launcher = Get-Command python.exe -ErrorAction SilentlyContinue
    $arguments = @('-u', 'app.py')
}
if ($null -eq $launcher) {
    throw 'Python was not found. Install Python 3 and add py.exe or python.exe to PATH.'
}

Set-Location -LiteralPath $ProjectRoot
$env:PYTHONUNBUFFERED = '1'
while ($true) {
    $startedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$startedAt] Starting Local Project Console with $($launcher.Source)" | Out-File -LiteralPath $logPath -Append -Encoding utf8
    & $launcher.Source @arguments 2>&1 | Out-File -LiteralPath $logPath -Append -Encoding utf8
    $processExitCode = $LASTEXITCODE
    $stoppedAt = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$stoppedAt] Console process exited with code $processExitCode; restarting in 3 seconds." | Out-File -LiteralPath $logPath -Append -Encoding utf8
    Start-Sleep -Seconds 3
}
