param(
    [int]$AdminPort = 18767,
    [int]$RemotePort = 18766,
    [int]$TimeoutSeconds = 8
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$benchmarkRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'codex-session-manager-benchmark-' + [guid]::NewGuid().ToString('N')
)
$dataRoot = Join-Path $benchmarkRoot 'data'
$runtimeRoot = Join-Path $benchmarkRoot 'runtime'
$logRoot = Join-Path $benchmarkRoot 'logs'
New-Item -ItemType Directory -Force -Path $dataRoot, $runtimeRoot, $logRoot | Out-Null

$previousAdminPort = $env:CSM_ADMIN_PORT
$previousRemotePort = $env:CSM_REMOTE_PORT
$env:CSM_ADMIN_PORT = [string]$AdminPort
$env:CSM_REMOTE_PORT = [string]$RemotePort
$arguments = @(
    'app.py', '--service', '--mode', 'installed',
    '--data-dir', $dataRoot,
    '--runtime-dir', $runtimeRoot,
    '--log-dir', $logRoot
)
$stopArguments = @(
    'app.py', '--stop', '--mode', 'installed',
    '--data-dir', $dataRoot,
    '--runtime-dir', $runtimeRoot,
    '--log-dir', $logRoot
)

$watch = [Diagnostics.Stopwatch]::StartNew()
$process = $null
try {
    $process = Start-Process `
        -FilePath 'python' `
        -ArgumentList $arguments `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru
    $health = $null
    $firstHealthMs = $null
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        try {
            $health = Invoke-RestMethod `
                -Uri "http://127.0.0.1:$AdminPort/api/health" `
                -TimeoutSec 1
            if ($null -eq $firstHealthMs) {
                $firstHealthMs = [math]::Round($watch.Elapsed.TotalMilliseconds)
            }
            if ([bool]$health.ready) {
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 50
        }
    }
    $watch.Stop()
    if ($null -eq $health) {
        throw "No health response within $TimeoutSeconds seconds. Logs: $logRoot"
    }
    $process.Refresh()
    [pscustomobject]@{
        firstHealthMs = $firstHealthMs
        readyMs = [math]::Round($watch.Elapsed.TotalMilliseconds)
        hostPid = $process.Id
        hostWorkingSetMb = [math]::Round($process.WorkingSet64 / 1MB, 1)
        ready = [bool]$health.ready
        codexConnected = [bool]$health.codex.connected
        codexConnecting = [bool]$health.codex.connecting
        benchmarkRoot = $benchmarkRoot
    } | ConvertTo-Json
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        & python @stopArguments | Out-Null
        $process.WaitForExit(3000) | Out-Null
    }
    $env:CSM_ADMIN_PORT = $previousAdminPort
    $env:CSM_REMOTE_PORT = $previousRemotePort
}
