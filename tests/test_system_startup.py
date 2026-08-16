from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SystemStartupContractTests(unittest.TestCase):
    def test_task_runs_for_the_interactive_user_and_restarts_on_failure(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(encoding="utf-8")

        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("-RestartCount 999", script)
        self.assertIn("-RestartInterval (New-TimeSpan -Minutes 1)", script)
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        for action in ("Install", "Status", "Start", "Restart", "Uninstall"):
            self.assertIn(f"'{action}'", script)

    def test_restart_replaces_stray_console_processes_without_expensive_tcp_cmdlets(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(
            encoding="utf-8"
        )

        for contract in (
            "$consolePorts = @(8765, 8766)",
            "function Get-ConsoleListenerProcessIds",
            "netstat.exe",
            "function Stop-StrayConsoleProcesses",
            "taskkill.exe",
        ):
            self.assertIn(contract, script)
        self.assertNotIn("Get-NetTCPConnection", script)
        restart = script.split("'Restart' {", 1)[1].split("'Uninstall' {", 1)[0]
        self.assertLess(
            restart.index("Stop-StrayConsoleProcesses"),
            restart.index("Start-ConsoleTaskFast"),
        )

    def test_service_runner_uses_project_local_runtime_logs(self) -> None:
        script = (ROOT / "scripts" / "run-console-service.ps1").read_text(encoding="utf-8")

        self.assertIn(".runtime\\system-startup", script)
        self.assertIn("console-service.log", script)
        self.assertIn("Get-Command py.exe", script)
        self.assertIn("Get-Command python.exe", script)
        self.assertIn("'app.py'", script)
        self.assertIn("Out-File -LiteralPath $logPath -Append -Encoding utf8", script)
        self.assertIn("while ($true)", script)
        self.assertIn("Start-Sleep -Seconds 3", script)
        self.assertIn("restarting in 3 seconds", script)

    def test_one_click_installers_delegate_to_the_management_script(self) -> None:
        install = (ROOT / "install-system-startup.bat").read_text(encoding="utf-8")
        uninstall = (ROOT / "uninstall-system-startup.bat").read_text(encoding="utf-8")

        self.assertIn("manage-system-startup.ps1", install)
        self.assertIn("-Action Install -StartNow", install)
        self.assertIn("manage-system-startup.ps1", uninstall)
        self.assertIn("-Action Uninstall", uninstall)

    def test_desktop_launcher_uses_the_system_task_instead_of_starting_python(self) -> None:
        launcher = (ROOT / "start-console.bat").read_text(encoding="utf-8")

        self.assertIn("manage-system-startup.ps1", launcher)
        self.assertIn("-Action Start", launcher)
        self.assertIn("/api/health", launcher)
        self.assertIn("findstr.exe", launcher)
        self.assertIn("http://127.0.0.1:8765/", launcher)
        self.assertNotIn("py app.py", launcher)
        self.assertLess(
            launcher.index("/api/health"),
            launcher.index("manage-system-startup.ps1"),
        )

    def test_start_action_checks_lightweight_health_before_loading_task_scheduler(self) -> None:
        script = (ROOT / "scripts" / "manage-system-startup.ps1").read_text(
            encoding="utf-8"
        )
        start = script.split("'Start' {", 1)[1].split("'Restart' {", 1)[0]

        self.assertIn("/api/health", script)
        self.assertLess(start.index("Test-ConsoleHealth"), start.index("Test-ConsoleTaskExists"))
        self.assertNotIn("Get-ConsoleTask", start)

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
