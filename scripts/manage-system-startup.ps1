[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Start', 'Restart', 'Stop', 'Uninstall')]
    [string]$Action = 'Install',
    [string]$TaskName = 'Local Project Console',
    [string]$ProjectRoot = '',
    [string]$ServiceExecutable = '',
    [string]$DataDirectory = '',
    [string]$RuntimeDirectory = '',
    [string]$LogDirectory = '',
    [ValidateSet('source', 'installed', 'portable')]
    [string]$RuntimeMode = 'installed',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$appPath = Join-Path $ProjectRoot 'app.py'
$healthUrl = 'http://127.0.0.1:8765/api/health'
$consolePorts = @(8765, 8766, 8767)

function Get-PythonServiceExecutable {
    $pythonPath = ''
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        $pythonPath = [string](& $launcher.Source -3 -c 'import sys; print(sys.executable)')
        if ($LASTEXITCODE -ne 0) { $pythonPath = '' }
    }
    if ([string]::IsNullOrWhiteSpace($pythonPath)) {
        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -eq $python) {
            throw 'Python 3 was not found. Install Python and make py.exe or python.exe available.'
        }
        $pythonPath = $python.Source
    }
    $pythonPath = [System.IO.Path]::GetFullPath($pythonPath.Trim())
    $pythonwPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonwPath -PathType Leaf) {
        return $pythonwPath
    }
    return $pythonPath
}

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
        if (
            $commandLine -notmatch '(?i)(^|[\\/"\s])app\.py(["\s]|$)' -and
            $commandLine -notmatch '(?i)(^|[\\/"\s])lpc-service\.exe(["\s]|$)'
        ) {
            throw "A Local Project Console port is held by another process (PID $processId); refusing to terminate it."
        }
        $previousErrorPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'SilentlyContinue'
            & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
            $taskkillExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorPreference
        }
        if ($taskkillExitCode -ne 0 -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
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
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Get-ConsoleTask {
    return Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Get-InstalledTaskAction {
    $task = Get-ConsoleTask
    if ($null -eq $task -or $task.Actions.Count -eq 0) { return $null }
    return $task.Actions[0]
}

function Invoke-LegacyMigration {
    param(
        [string]$LegacyRoot
    )
    if ([string]::IsNullOrWhiteSpace($LegacyRoot)) { return }
    $legacyProjects = Join-Path $LegacyRoot 'projects.json'
    $legacyDatabase = Join-Path $LegacyRoot 'watchdog.db'
    if (-not (Test-Path -LiteralPath $legacyProjects) -and -not (Test-Path -LiteralPath $legacyDatabase)) {
        return
    }
    if (
        (Test-Path -LiteralPath (Join-Path $DataDirectory 'projects.json')) -or
        (Test-Path -LiteralPath (Join-Path $DataDirectory 'watchdog.db'))
    ) {
        return
    }
    & $ServiceExecutable `
        --migrate-from $LegacyRoot `
        --data-dir $DataDirectory `
        --runtime-dir $RuntimeDirectory `
        --log-dir $LogDirectory `
        --mode $RuntimeMode
    if ($LASTEXITCODE -ne 0) {
        throw "Legacy data migration failed with exit code $LASTEXITCODE."
    }
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

function Restore-PreviousConsoleTask {
    param(
        [string]$TaskXml,
        [bool]$WasHealthy
    )
    if (-not [string]::IsNullOrWhiteSpace($TaskXml)) {
        Register-ScheduledTask -TaskName $TaskName -Xml $TaskXml -Force | Out-Null
    } elseif (Test-ConsoleTaskExists) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    if ($WasHealthy -and (Test-ConsoleTaskExists)) {
        Start-ConsoleTaskFast
        Wait-ConsoleHealth -Expected $true -TimeoutSeconds 15 | Out-Null
    }
}

switch ($Action) {
    'Install' {
        $packaged = -not [string]::IsNullOrWhiteSpace($ServiceExecutable)
        if ($packaged) {
            $ServiceExecutable = [System.IO.Path]::GetFullPath($ServiceExecutable)
            if (-not (Test-Path -LiteralPath $ServiceExecutable -PathType Leaf)) {
                throw "Packaged service executable was not found: $ServiceExecutable"
            }
            foreach ($requiredDirectory in @($DataDirectory, $RuntimeDirectory, $LogDirectory)) {
                if ([string]::IsNullOrWhiteSpace($requiredDirectory)) {
                    throw 'Packaged startup requires data, runtime, and log directories.'
                }
            }
            $DataDirectory = [System.IO.Path]::GetFullPath($DataDirectory)
            $RuntimeDirectory = [System.IO.Path]::GetFullPath($RuntimeDirectory)
            $LogDirectory = [System.IO.Path]::GetFullPath($LogDirectory)
        } elseif (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) {
            throw "Application entry point was not found: $appPath"
        }

        $oldTask = Get-ConsoleTask
        $oldTaskXml = if ($null -ne $oldTask) { Export-ScheduledTask -TaskName $TaskName } else { '' }
        $oldWasHealthy = Test-ConsoleHealth
        $oldAction = Get-InstalledTaskAction
        $legacyRoot = ''
        $switchingRuntime = $false
        if ($packaged -and $null -ne $oldAction) {
            $oldExecute = [string]$oldAction.Execute
            $switchingRuntime = [System.IO.Path]::GetFullPath($oldExecute) -ne $ServiceExecutable
            $candidate = [string]$oldAction.WorkingDirectory
            if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath (Join-Path $candidate 'app.py'))) {
                $legacyRoot = [System.IO.Path]::GetFullPath($candidate)
            }
        }

        try {
            if ($switchingRuntime) {
                Stop-ConsoleTaskFast
                Start-Sleep -Milliseconds 300
                Stop-StrayConsoleProcesses
                Wait-ConsoleHealth -Expected $false -TimeoutSeconds 5 | Out-Null
            }

            $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            if ($packaged) {
                Invoke-LegacyMigration -LegacyRoot $legacyRoot
                $taskArguments = "--service --data-dir `"$DataDirectory`" --runtime-dir `"$RuntimeDirectory`" --log-dir `"$LogDirectory`" --mode $RuntimeMode"
                $taskAction = New-ScheduledTaskAction `
                    -Execute $ServiceExecutable `
                    -Argument $taskArguments `
                    -WorkingDirectory (Split-Path -Parent $ServiceExecutable)
            } else {
                $pythonServiceExecutable = Get-PythonServiceExecutable
                $taskArguments = "-u `"$appPath`" --service"
                $taskAction = New-ScheduledTaskAction -Execute $pythonServiceExecutable -Argument $taskArguments -WorkingDirectory $ProjectRoot
            }
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
                -Description 'Starts the lightweight project console first, then its isolated session and remote runtime.'
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
        } catch {
            Restore-PreviousConsoleTask -TaskXml $oldTaskXml -WasHealthy $oldWasHealthy
            throw
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
            throw 'The console did not become healthy within 15 seconds. Use the desktop app to open the log directory.'
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
            throw 'The console did not become healthy within 15 seconds. Use the desktop app to open the log directory.'
        }
        Write-Host '[LPC] Console restarted and is healthy at http://127.0.0.1:8765/'
    }
    'Stop' {
        if (Test-ConsoleTaskExists) {
            Stop-ConsoleTaskFast
            Start-Sleep -Milliseconds 300
        }
        Stop-StrayConsoleProcesses
        if (-not (Wait-ConsoleHealth -Expected $false -TimeoutSeconds 5)) {
            throw 'The console did not stop cleanly.'
        }
        Write-Host '[LPC] Console background service stopped.'
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
