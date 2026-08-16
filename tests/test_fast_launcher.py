from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lpc_fast_launcher",
    ROOT / "scripts" / "launch_console.py",
)
assert SPEC is not None and SPEC.loader is not None
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class FastLauncherTests(unittest.TestCase):
    def test_healthy_console_opens_without_touching_task_scheduler(self) -> None:
        with (
            patch.object(LAUNCHER, "console_is_ready", return_value=True),
            patch.object(LAUNCHER, "start_scheduled_task") as start_task,
            patch.object(LAUNCHER.webbrowser, "open", return_value=True) as open_page,
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 0)
        start_task.assert_not_called()
        open_page.assert_called_once_with(LAUNCHER.CONSOLE_URL)

    def test_normal_cold_start_uses_fast_task_path_before_recovery(self) -> None:
        with (
            patch.object(LAUNCHER, "console_is_ready", return_value=False),
            patch.object(LAUNCHER, "start_scheduled_task", return_value=True) as start_task,
            patch.object(LAUNCHER, "wait_until_ready", return_value=True) as wait_ready,
            patch.object(LAUNCHER, "recover_with_management_script") as recover,
            patch.object(LAUNCHER.webbrowser, "open", return_value=True),
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 0)
        start_task.assert_called_once_with()
        wait_ready.assert_called_once_with(1.25)
        recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
