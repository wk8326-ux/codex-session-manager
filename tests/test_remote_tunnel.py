from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from remote.tunnel import FrpTunnelManager


class FrpTunnelManagerTests(unittest.TestCase):
    def test_missing_runtime_files_leave_tunnel_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = FrpTunnelManager(
                root / "frpc.exe", root / "frpc.toml", root / "frpc.log"
            )

            self.assertFalse(manager.start())
            self.assertEqual(manager.status()["state"], "not-configured")

    def test_start_and_stop_own_the_frpc_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "frpc.exe"
            config = root / "frpc.toml"
            executable.write_bytes(b"binary")
            config.write_text("serverAddr = 'example'", encoding="utf-8")
            process = MagicMock()
            process.pid = 1234
            process.poll.return_value = None
            manager = FrpTunnelManager(executable, config, root / "frpc.log")

            with patch("remote.tunnel.subprocess.Popen", return_value=process) as popen:
                self.assertTrue(manager.start())
                self.assertTrue(manager.status()["running"])
                self.assertEqual(manager.status()["pid"], 1234)
                popen.assert_called_once()
                manager.stop()

            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=5)

    def test_stop_kills_a_child_that_does_not_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "frpc.exe"
            config = root / "frpc.toml"
            executable.write_bytes(b"binary")
            config.write_text("serverAddr = 'example'", encoding="utf-8")
            process = MagicMock()
            process.pid = 1234
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("frpc", 5), None]
            manager = FrpTunnelManager(executable, config, root / "frpc.log")

            with patch("remote.tunnel.subprocess.Popen", return_value=process):
                manager.start()
                manager.stop()

            process.kill.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
