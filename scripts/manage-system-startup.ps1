[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Restart', 'Uninstall')]
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
$healthUrl = 'http://127.0.0.1:8765/api/shell/project-summary'

function Test-ConsoleHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
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
    'Restart' {
        $task = Get-ConsoleTask
        if ($null -eq $task) { throw "Scheduled task is not installed: $TaskName" }
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Wait-ConsoleHealth -Expected $false -TimeoutSeconds 10 | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        if (-not (Wait-ConsoleHealth -Expected $true -TimeoutSeconds 30)) {
            throw 'The console did not become healthy within 30 seconds. Check .runtime\system-startup\console-service.log.'
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
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host '[LPC] Scheduled task removed.'
    }
}
