[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDirectory,
    [string]$TaskName = 'Local Project Console'
)

$ErrorActionPreference = 'Stop'
$expectedExecutable = [System.IO.Path]::GetFullPath(
    (Join-Path $InstallDirectory 'backend\lpc-service.exe')
)
$consolePorts = @(8765)

function Get-ConsoleListeners {
    $netstatPath = Join-Path $env:SystemRoot 'System32\netstat.exe'
    $listeners = foreach ($line in (& $netstatPath -ano -p TCP)) {
        $parts = @($line.Trim() -split '\s+')
        if ($parts.Count -lt 5 -or $parts[0] -ne 'TCP' -or $parts[3] -ne 'LISTENING') {
            continue
        }
        $separator = ([string]$parts[1]).LastIndexOf(':')
        if ($separator -lt 0) { continue }
        $port = 0
        $owner = 0
        if (
            [int]::TryParse(([string]$parts[1]).Substring($separator + 1), [ref]$port) -and
            $port -in $consolePorts -and
            [int]::TryParse([string]$parts[-1], [ref]$owner)
        ) {
            [pscustomobject]@{ Port = $port; ProcessId = $owner }
        }
    }
    return @($listeners)
}

function Stop-InstalledRuntimeProcesses {
    $deadline = (Get-Date).AddSeconds(10)
    do {
        $matched = @()
        foreach ($process in @(Get-Process lpc-service -ErrorAction SilentlyContinue)) {
            $actualExecutable = ''
            try { $actualExecutable = [string]$process.Path } catch {}
            if ([string]::IsNullOrWhiteSpace($actualExecutable)) { continue }
            if (
                ([System.IO.Path]::GetFullPath($actualExecutable)).Equals(
                    $expectedExecutable,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) {
                $matched += $process
            }
        }
        if ($matched.Count -eq 0) { return }
        foreach ($process in $matched) {
            # Every packaged runtime process has already been matched by its exact
            # executable path, so terminate each one directly. Recursive taskkill
            # can hang while traversing an auxiliary process that is paging in.
            # A process that is already leaving can briefly reject OpenProcess even
            # though it disappears moments later. Let the bounded retry loop decide.
            try { $process.Kill() } catch {}
        }
        Start-Sleep -Milliseconds 150
    } while ((Get-Date) -lt $deadline)
    throw 'The installed Local Project Console runtime did not exit within 10 seconds.'
}

$schtasksPath = Join-Path $env:SystemRoot 'System32\schtasks.exe'
& $schtasksPath /End /TN $TaskName 2>$null | Out-Null
Start-Sleep -Milliseconds 250
Stop-InstalledRuntimeProcesses

$deadline = (Get-Date).AddSeconds(20)
do {
    $listeners = @(Get-ConsoleListeners)
    if ($listeners.Count -eq 0) { exit 0 }
    Start-Sleep -Milliseconds 200
} while ((Get-Date) -lt $deadline)

$summary = ($listeners | ForEach-Object { "$($_.Port) (PID $($_.ProcessId))" }) -join ', '
Write-Error "Local Project Console ports are still occupied: $summary"
exit 1
