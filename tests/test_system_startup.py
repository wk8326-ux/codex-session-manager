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
        for action in ("Install", "Status", "Restart", "Uninstall"):
            self.assertIn(f"'{action}'", script)

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


if __name__ == "__main__":
    unittest.main()
