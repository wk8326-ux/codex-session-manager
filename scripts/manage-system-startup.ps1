[CmdletBinding()]
param(
    [ValidateSet('Install', 'Validate', 'Status', 'Start', 'Restart', 'Stop', 'Uninstall')]
    [string]$Action = 'Install',
    [string]$TaskName = 'Local Project Console',
    [string]$ProjectRoot = '',
    [string]$ServiceExecutable = '',
    [string]$DataDirectory = '',
    [string]$RuntimeDirectory = '',
    [string]$LogDirectory = '',
    [ValidateSet('source', 'installed', 'portable')]
    [string]$RuntimeMode = 'installed',
    [string]$ExpectedVersion = '',
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

function ConvertFrom-LpcVerbatimPath {
    param(
        [AllowEmptyString()]
        [string]$Path
    )
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    if ($Path.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return '\\' + $Path.Substring(8)
    }
    if ($Path.StartsWith('\\?\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return $Path.Substring(4)
    }
    return $Path
}

$scriptDirectory = ConvertFrom-LpcVerbatimPath -Path $PSScriptRoot
$ProjectRoot = ConvertFrom-LpcVerbatimPath -Path $ProjectRoot
$ServiceExecutable = ConvertFrom-LpcVerbatimPath -Path $ServiceExecutable
$DataDirectory = ConvertFrom-LpcVerbatimPath -Path $DataDirectory
$RuntimeDirectory = ConvertFrom-LpcVerbatimPath -Path $RuntimeDirectory
$LogDirectory = ConvertFrom-LpcVerbatimPath -Path $LogDirectory
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $scriptDirectory
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$appPath = Join-Path $ProjectRoot 'app.py'
$healthUrl = 'http://127.0.0.1:8765/api/health'
$consolePorts = @(8765)

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

function Get-ConsoleHealthMetadata {
    try {
        $curlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
        $response = & $curlPath --fail --silent --max-time 1 $healthUrl 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $health = [string]::Join('', @($response)) | ConvertFrom-Json
        if ([string]$health.service -ne 'local-project-console') { return $null }
        return $health
    } catch {
        return $null
    }
}

function Test-ConsoleHealth {
    param([switch]$AnyVersion)
    $health = Get-ConsoleHealthMetadata
    if ($null -eq $health -or -not [bool]$health.ready) { return $false }
    if ($AnyVersion -or [string]::IsNullOrWhiteSpace($ExpectedVersion)) {
        return $true
    }
    try {
        return (
            [string]$health.version -eq $ExpectedVersion -and
            [string]$health.mode -eq $RuntimeMode -and
            [string]$health.role -in @('', 'core') -and
            [int]$health.ports.admin -eq 8765
        )
    } catch {
        return $false
    }
}

function Get-ConsolePidFileProcessIds {
    $processIds = @()
    $health = Get-ConsoleHealthMetadata
    $healthPid = 0
    if (
        $null -ne $health -and
        [int]::TryParse([string]$health.pid, [ref]$healthPid) -and
        $healthPid -gt 0
    ) {
        $processIds += $healthPid
    }
    if ([string]::IsNullOrWhiteSpace($RuntimeDirectory)) {
        return @($processIds | Select-Object -Unique)
    }
    $lockDirectory = Join-Path $RuntimeDirectory 'system-startup'
    foreach ($name in @('console.pid')) {
        $path = Join-Path $lockDirectory $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $value = 0
        if ([int]::TryParse(([string](Get-Content -Raw -LiteralPath $path)).Trim(), [ref]$value) -and $value -gt 0) {
            $processIds += $value
        }
    }
    return @($processIds | Select-Object -Unique)
}

function Test-ConsoleProcessOwnership {
    param(
        [int]$ProcessId,
        [int[]]$KnownProcessIds
    )
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $processName = [string]$process.ProcessName
    if (
        $ProcessId -in $KnownProcessIds -and
        $processName -in @('lpc-service', 'python', 'pythonw')
    ) {
        return $true
    }

    if (-not [string]::IsNullOrWhiteSpace($ServiceExecutable)) {
        $expectedExecutable = [System.IO.Path]::GetFullPath($ServiceExecutable)
        $actualExecutable = ''
        try { $actualExecutable = [string]$process.Path } catch {}
        $pathMatches = (
            -not [string]::IsNullOrWhiteSpace($actualExecutable) -and
            ([System.IO.Path]::GetFullPath($actualExecutable)).Equals(
                $expectedExecutable,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        )
        if ($pathMatches) { return $true }
    }

    $commandLine = ''
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        $commandLine = [string]$processInfo.CommandLine
    } catch {}
    $runtimeSwitch = $commandLine -match '(?i)--(?:core|service|session-manager|runtime-worker)(?:\s|$)'
    if (-not [string]::IsNullOrWhiteSpace($ServiceExecutable)) {
        $expectedExecutable = [System.IO.Path]::GetFullPath($ServiceExecutable)
        $commandMatches = (
            $commandLine.IndexOf($expectedExecutable, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        )
        return $processName -eq 'lpc-service' -and $runtimeSwitch -and $commandMatches
    }
    $sourceMatches = (
        $commandLine.IndexOf($appPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    )
    return $processName -in @('python', 'pythonw') -and $runtimeSwitch -and $sourceMatches
}

function Wait-ConsoleProcessExit {
    param(
        [int]$ProcessId,
        [int]$TimeoutMilliseconds = 10000
    )
    $deadline = (Get-Date).AddMilliseconds($TimeoutMilliseconds)
    do {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Stop-StrayConsoleProcesses {
    $processIds = @(Get-ConsoleListenerProcessIds)
    $knownProcessIds = @(Get-ConsolePidFileProcessIds)
    foreach ($processId in $processIds) {
        if ($processId -eq $PID) { continue }
        $isConsoleRuntime = Test-ConsoleProcessOwnership -ProcessId $processId -KnownProcessIds $knownProcessIds
        if ($null -eq $isConsoleRuntime) { continue }
        if (-not $isConsoleRuntime) {
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
        if (-not (Wait-ConsoleProcessExit -ProcessId $processId)) {
            throw "Could not stop stale Local Project Console process $processId (taskkill exit code $taskkillExitCode)."
        }
    }
}

function Invoke-ScheduledTaskCommand {
    param([ValidateSet('Query', 'End', 'Run')][string]$Verb)
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & schtasks.exe "/$Verb" /TN $TaskName 2>$null | Out-Null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorPreference
    }
}

function Test-ConsoleTaskExists {
    return (Invoke-ScheduledTaskCommand -Verb 'Query') -eq 0
}

function Stop-ConsoleTaskFast {
    Invoke-ScheduledTaskCommand -Verb 'End' | Out-Null
}

function Start-ConsoleTaskFast {
    if ((Invoke-ScheduledTaskCommand -Verb 'Run') -ne 0) {
        throw "Scheduled task could not be started: $TaskName"
    }
}

function Wait-ConsoleHealth {
    param(
        [bool]$Expected,
        [int]$TimeoutSeconds,
        [switch]$AnyVersion
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ((Test-ConsoleHealth -AnyVersion:$AnyVersion) -eq $Expected) { return $true }
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

function Assert-InstalledTaskMatches {
    $task = Get-ConsoleTask
    if ($null -eq $task -or $task.Actions.Count -eq 0) {
        throw "Scheduled task is not installed: $TaskName"
    }
    $taskAction = $task.Actions[0]
    if ([string]::IsNullOrWhiteSpace($ServiceExecutable)) {
        throw 'Task validation requires ServiceExecutable.'
    }
    $expectedExecutable = [System.IO.Path]::GetFullPath($ServiceExecutable)
    $actualExecutable = [System.IO.Path]::GetFullPath([string]$taskAction.Execute)
    if (-not $actualExecutable.Equals($expectedExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Scheduled task executable is stale: $actualExecutable"
    }
    $arguments = [string]$taskAction.Arguments
    foreach ($required in @('--core', $DataDirectory, $RuntimeDirectory, $LogDirectory, "--mode $RuntimeMode")) {
        if ($arguments.IndexOf($required, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw "Scheduled task arguments are stale: missing $required"
        }
    }
    if ([int]$task.Settings.Priority -ne 4) {
        throw "Scheduled task priority is stale: $($task.Settings.Priority)"
    }
}

function Test-InstalledTaskMatches {
    try {
        Assert-InstalledTaskMatches
        return $true
    } catch {
        return $false
    }
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
        Wait-ConsoleHealth -Expected $true -TimeoutSeconds 15 -AnyVersion | Out-Null
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
        $oldWasHealthy = Test-ConsoleHealth -AnyVersion
        $oldWasCompatible = Test-ConsoleHealth
        $oldAction = Get-InstalledTaskAction
        $legacyRoot = ''
        $switchingRuntime = $false
        if ($packaged -and $null -ne $oldAction) {
            $switchingRuntime = -not (Test-InstalledTaskMatches)
            $candidate = [string]$oldAction.WorkingDirectory
            if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath (Join-Path $candidate 'app.py'))) {
                $legacyRoot = [System.IO.Path]::GetFullPath($candidate)
            }
        }

        try {
            if ($switchingRuntime -or ($StartNow -and $oldWasHealthy -and -not $oldWasCompatible)) {
                Stop-ConsoleTaskFast
                Start-Sleep -Milliseconds 300
                Stop-StrayConsoleProcesses
                Wait-ConsoleHealth -Expected $false -TimeoutSeconds 5 | Out-Null
            }

            $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            if ($packaged) {
                Invoke-LegacyMigration -LegacyRoot $legacyRoot
                $taskArguments = "--core --data-dir `"$DataDirectory`" --runtime-dir `"$RuntimeDirectory`" --log-dir `"$LogDirectory`" --mode $RuntimeMode"
                $taskAction = New-ScheduledTaskAction `
                    -Execute $ServiceExecutable `
                    -Argument $taskArguments `
                    -WorkingDirectory (Split-Path -Parent $ServiceExecutable)
            } else {
                $pythonServiceExecutable = Get-PythonServiceExecutable
                $taskArguments = "-u `"$appPath`" --core"
                $taskAction = New-ScheduledTaskAction -Execute $pythonServiceExecutable -Argument $taskArguments -WorkingDirectory $ProjectRoot
            }
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
            $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -Priority 4 `
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
                    if (-not (Wait-ConsoleHealth -Expected $true -TimeoutSeconds 120)) {
                        throw 'The scheduled task started, but the console health endpoint did not become available within 120 seconds.'
                    }
                    Write-Host '[LPC] Console is available at http://127.0.0.1:8765/'
                }
            }
        } catch {
            Restore-PreviousConsoleTask -TaskXml $oldTaskXml -WasHealthy $oldWasHealthy
            throw
        }
    }
    'Validate' {
        Assert-InstalledTaskMatches
        Write-Host '[LPC] Scheduled task registration matches the packaged runtime.'
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
