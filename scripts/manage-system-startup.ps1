[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Start', 'Restart', 'Uninstall')]
    [string]$Action = 'Install',
    [string]$TaskName = 'Local Project Console',
    [string]$ProjectRoot = '',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$runnerPath = Join-Path $ProjectRoot 'scripts\run-console-service.ps1'
$healthUrl = 'http://127.0.0.1:8765/api/health'
$consolePorts = @(8765, 8766)

function Get-ConsoleListenerProcessIds {
    $netstatPath = Join-Path $env:SystemRoot 'System32\netstat.exe'
    $processIds = foreach ($line in (& $netstatPath -ano -p TCP)) {
        $parts = @($line.Trim() -split '\s+')
        if ($parts.Count -lt 5 -or $parts[0] -ne 'TCP' -or $parts[3] -ne 'LISTENING') {
            continue
        }
        $localEndpoint = [string]$parts[1]
        $separator = $localEndpoint.LastIndexOf(':')
        if ($separator -lt 0) { continue }
        $port = 0
        $owner = 0
        if (
            [int]::TryParse($localEndpoint.Substring($separator + 1), [ref]$port) -and
            $port -in $consolePorts -and
            [int]::TryParse([string]$parts[-1], [ref]$owner)
        ) {
            $owner
        }
    }
    return @($processIds | Select-Object -Unique)
}

function Test-ConsoleHealth {
    try {
        $curlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
        $response = & $curlPath --fail --silent --max-time 1 $healthUrl 2>$null
        return $LASTEXITCODE -eq 0 -and [string]::Join('', @($response)).Contains('local-project-console')
    } catch {
        return $false
    }
}

function Stop-StrayConsoleProcesses {
    $processIds = @(Get-ConsoleListenerProcessIds)
    foreach ($processId in $processIds) {
        if ($processId -eq $PID) { continue }
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if ($null -eq $processInfo) { continue }
        $commandLine = [string]$processInfo.CommandLine
        if ($commandLine -notmatch '(?i)(^|[\\/"\s])app\.py(["\s]|$)') {
            throw "Port 8765 or 8766 is held by another process (PID $processId); refusing to terminate it."
        }
        & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            throw "Could not stop stale Local Project Console process $processId."
        }
    }
}

function Test-ConsoleTaskExists {
    & schtasks.exe /Query /TN $TaskName 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Stop-ConsoleTaskFast {
    & schtasks.exe /End /TN $TaskName 2>$null | Out-Null
}

function Start-ConsoleTaskFast {
    & schtasks.exe /Run /TN $TaskName 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Scheduled task could not be started: $TaskName"
    }
}

function Wait-ConsoleHealth {
    param(
        [bool]$Expected,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ((Test-ConsoleHealth) -eq $Expected) { return $true }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-ConsoleTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Wait-ConsoleTaskState {
    param(
        [string]$Expected,
        [int]$TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ConsoleTask
        if ($null -ne $task -and [string]$task.State -eq $Expected) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return $false
}

switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
            throw "Service runner was not found: $runnerPath"
        }
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
        $taskArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runnerPath`" -ProjectRoot `"$ProjectRoot`""
        $taskAction = New-ScheduledTaskAction -Execute $powerShellPath -Argument $taskArguments -WorkingDirectory $ProjectRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
            -MultipleInstances IgnoreNew
        $definition = New-ScheduledTask `
            -Action $taskAction `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Keeps Local Project Console, session monitoring, remote PWA, and FRP available after Windows logon.'
        Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
        Write-Host "[LPC] Scheduled task installed for $currentUser."
        Write-Host "[LPC] Project root: $ProjectRoot"
        if ($StartNow) {
            if (Test-ConsoleHealth) {
                Write-Host '[LPC] A console process is already healthy. Close it and run Restart to transfer ownership to Task Scheduler.'
            } else {
                Start-ScheduledTask -TaskName $TaskName
                if (-not (Wait-ConsoleHealth -Expected $true -TimeoutSeconds 30)) {
                    throw 'The scheduled task started, but the console health endpoint did not become available within 30 seconds.'
                }
                Write-Host '[LPC] Console is available at http://127.0.0.1:8765/'
            }
        }
    }
    'Status' {
        $task = Get-ConsoleTask
        if ($null -eq $task) {
            Write-Host '[LPC] Scheduled task is not installed.'
            exit 1
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        [pscustomobject]@{
            TaskName = $TaskName
            TaskState = $task.State
            LastRunTime = $info.LastRunTime
            LastTaskResult = $info.LastTaskResult
            NextRunTime = $info.NextRunTime
            ConsoleHealthy = Test-ConsoleHealth
            ConsoleUrl = 'http://127.0.0.1:8765/'
        } | Format-List
    }
    'Start' {
        if (Test-ConsoleHealth) {
            Write-Host '[LPC] Console is already running at http://127.0.0.1:8765/'
            break
        }
        if (-not (Test-ConsoleTaskExists)) { throw "Scheduled task is not installed: $TaskName" }
        Stop-ConsoleTaskFast
        Start-Sleep -Milliseconds 200
        Stop-StrayConsoleProcesses
        if (-not (Wait-ConsoleHealth -Expected $false -TimeoutSeconds 5)) {
            throw 'The previous console instance did not stop cleanly.'
        }
        Start-ConsoleTaskFast
        if (-not (Wait-ConsoleHealth -Expected $true -TimeoutSeconds 15)) {
            throw 'The console did not become healthy within 15 seconds. Check .runtime\system-startup\console-service.log.'
        }
        Write-Host '[LPC] Console started and is healthy at http://127.0.0.1:8765/'
    }
    'Restart' {
        if (-not (Test-ConsoleTaskExists)) { throw "Scheduled task is not installed: $TaskName" }
        Stop-ConsoleTaskFast
        Start-Sleep -Milliseconds 200
        Stop-StrayConsoleProcesses
        if (-not (Wait-ConsoleHealth -Expected $false -TimeoutSeconds 5)) {
            throw 'The previous console instance did not stop cleanly.'
        }
        Start-ConsoleTaskFast
        if (-not (Wait-ConsoleHealth -Expected $true -TimeoutSeconds 15)) {
            throw 'The console did not become healthy within 15 seconds. Check .runtime\system-startup\console-service.log.'
        }
        Write-Host '[LPC] Console restarted and is healthy at http://127.0.0.1:8765/'
    }
    'Uninstall' {
        $task = Get-ConsoleTask
        if ($null -eq $task) {
            Write-Host '[LPC] Scheduled task is already absent.'
            break
        }
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Wait-ConsoleTaskState -Expected 'Ready' -TimeoutSeconds 10 | Out-Null
        Start-Sleep -Milliseconds 500
        Stop-StrayConsoleProcesses
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host '[LPC] Scheduled task removed.'
    }
}
