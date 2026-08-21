from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SystemStartupContractTests(unittest.TestCase):
    def test_task_runs_for_the_interactive_user_and_restarts_on_failure(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(encoding="utf-8")

        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("function Get-PythonServiceExecutable", script)
        self.assertIn("New-ScheduledTaskAction", script)
        self.assertIn("--core", script)
        self.assertIn("-RestartCount 999", script)
        self.assertIn("-RestartInterval (New-TimeSpan -Minutes 1)", script)
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("-Priority 4", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        for action in (
            "Install",
            "Validate",
            "Status",
            "Start",
            "Restart",
            "Stop",
            "Uninstall",
        ):
            self.assertIn(f"'{action}'", script)

        for contract in (
            "ConvertFrom-LpcVerbatimPath",
            "\\\\?\\UNC\\",
            "\\\\?\\",
            "ServiceExecutable",
            "DataDirectory",
            "RuntimeDirectory",
            "LogDirectory",
            "Invoke-LegacyMigration",
            "Restore-PreviousConsoleTask",
            "Assert-InstalledTaskMatches",
            "Test-InstalledTaskMatches",
            "Get-ConsolePidFileProcessIds",
            "Test-ConsoleProcessOwnership",
            "console.pid",
            "$isConsoleRuntime",
            "--migrate-from",
            "$taskkillExitCode",
            "$previousErrorPreference",
        ):
            self.assertIn(contract, script)
        self.assertIn("-TimeoutSeconds 120", script)
        self.assertIn("$task.Settings.Priority -ne 4", script)

        install = script.split("'Install' {", 1)[1].split("'Validate' {", 1)[0]
        self.assertIn(
            "$switchingRuntime = -not (Test-InstalledTaskMatches)", install
        )

    def test_restart_replaces_stray_console_processes_without_expensive_tcp_cmdlets(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(
            encoding="utf-8"
        )

        for contract in (
            "$consolePorts = @(8765)",
            "function Get-ConsoleListenerProcessIds",
            "netstat.exe",
            "function Stop-StrayConsoleProcesses",
            "function Wait-ConsoleProcessExit",
            "taskkill.exe",
        ):
            self.assertIn(contract, script)
        self.assertNotIn("Get-NetTCPConnection", script)
        stop_helper = script.split("function Stop-StrayConsoleProcesses", 1)[1].split(
            "function Invoke-ScheduledTaskCommand", 1
        )[0]
        self.assertIn("if ($null -eq $isConsoleRuntime) { continue }", stop_helper)
        self.assertIn("Wait-ConsoleProcessExit -ProcessId $processId", stop_helper)
        self.assertNotIn(
            "$taskkillExitCode -ne 0 -and (Get-Process", stop_helper
        )
        restart = script.split("'Restart' {", 1)[1].split("'Uninstall' {", 1)[0]
        self.assertLess(
            restart.index("Stop-StrayConsoleProcesses"),
            restart.index("Start-ConsoleTaskFast"),
        )

    def test_missing_scheduled_task_is_handled_by_exit_code(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(
            encoding="utf-8"
        )

        helper = script.split("function Invoke-ScheduledTaskCommand", 1)[1].split(
            "function Test-ConsoleTaskExists", 1
        )[0]
        self.assertIn("$ErrorActionPreference = 'SilentlyContinue'", helper)
        self.assertIn("$LASTEXITCODE", helper)
        exists = script.split("function Test-ConsoleTaskExists", 1)[1].split(
            "function Stop-ConsoleTaskFast", 1
        )[0]
        self.assertIn("Invoke-ScheduledTaskCommand -Verb 'Query'", exists)

    def test_service_mode_uses_project_local_runtime_logs(self) -> None:
        application = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertIn("--service", application)
        self.assertIn("--core", application)
        self.assertIn("console-service.log", application)
        self.assertIn("console-service.previous.log", application)

    def test_one_click_installers_delegate_to_the_management_script(self) -> None:
        install = (ROOT / "install-system-startup.bat").read_text(encoding="utf-8")
        uninstall = (ROOT / "uninstall-system-startup.bat").read_text(encoding="utf-8")

        self.assertIn("manage-system-startup.ps1", install)
        self.assertIn("-Action Install -StartNow", install)
        self.assertIn("manage-system-startup.ps1", uninstall)
        self.assertIn("-Action Uninstall", uninstall)

    def test_desktop_launcher_uses_the_fast_python_launcher(self) -> None:
        launcher = (ROOT / "start-console.bat").read_text(encoding="utf-8")
        fast_launcher = (ROOT / "scripts" / "launch_console.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("launch_console.py", launcher)
        self.assertIn("py.exe -3", launcher)
        self.assertNotIn("py app.py", launcher)
        self.assertIn("console_is_ready", fast_launcher)
        self.assertIn("schtasks.exe", fast_launcher)
        self.assertIn("wait_until_ready(1.25)", fast_launcher)
        self.assertLess(
            fast_launcher.index("start_scheduled_task()"),
            fast_launcher.index("recover_with_management_script()"),
        )

    def test_start_action_checks_lightweight_health_before_loading_task_scheduler(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(
            encoding="utf-8"
        )
        start = script.split("'Start' {", 1)[1].split("'Restart' {", 1)[0]

        self.assertIn("/api/health", script)
        self.assertLess(start.index("Test-ConsoleHealth"), start.index("Test-ConsoleTaskExists"))
        self.assertNotIn("Get-ConsoleTask", start)

    def test_packaged_runtime_takeover_uses_one_strict_health_contract(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(
            encoding="utf-8"
        )
        runtime = (ROOT / "desktop" / "src-tauri" / "src" / "runtime.rs").read_text(
            encoding="utf-8"
        )

        self.assertIn("[string]$ExpectedVersion = ''", script)
        health = script.split("function Test-ConsoleHealth", 1)[1].split(
            "function Get-ConsolePidFileProcessIds", 1
        )[0]
        for contract in (
            "$health.version",
            "$health.mode",
            "$health.ports.admin",
        ):
            self.assertIn(contract, health)

        pid_sources = script.split("function Get-ConsolePidFileProcessIds", 1)[1].split(
            "function Test-ConsoleProcessOwnership", 1
        )[0]
        self.assertIn("Get-ConsoleHealthMetadata", pid_sources)
        self.assertIn("$health.pid", pid_sources)

        ownership = script.split("function Test-ConsoleProcessOwnership", 1)[1].split(
            "function Stop-StrayConsoleProcesses", 1
        )[0]
        self.assertIn("if ($pathMatches) { return $true }", ownership)
        self.assertIn('"-ExpectedVersion"', runtime)
        self.assertIn("CURRENT_VERSION", runtime)

    def test_screenshot_installer_uses_a_pinned_flameshot_package(self) -> None:
        script = (ROOT / "scripts" / "install-screenshot-tool.ps1").read_text(
            encoding="utf-8"
        )
        launcher = (ROOT / "install-screenshot-tool.bat").read_text(
            encoding="utf-8"
        )

        for contract in (
            "Flameshot.Flameshot",
            "14.0.0",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
            "LPC_FLAMESHOT_PATH",
            "function Get-FlameshotVersion",
            "$installedVersion -ne $Version",
        ):
            self.assertIn(contract, script)
        self.assertIn("install-screenshot-tool.ps1", launcher)


if __name__ == "__main__":
    unittest.main()
