from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from watchdog.http_api import ApiResponse


class FakeWatchdogApi:
    def dispatch(self, method: str, path: str, query: dict, payload: object):
        return ApiResponse(200, {"method": method, "path": path})


class SessionManagerHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AdminHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str) -> tuple[int, bytes]:
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_health_identifies_independent_runtime(self) -> None:
        status, body = self.request("GET", "/api/health")
        payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "codex-session-manager")
        self.assertEqual(payload["role"], "session-manager")
        self.assertEqual(payload["ports"], {"admin": 8767, "remote": 8766})

    def test_project_console_routes_are_not_exposed(self) -> None:
        self.assertEqual(self.request("GET", "/api/projects")[0], 404)
        self.assertEqual(self.request("GET", "/api/shell/project-summary")[0], 404)

    def test_watchdog_route_uses_session_manager_dispatch(self) -> None:
        with patch.object(app, "WATCHDOG_API", FakeWatchdogApi()):
            status, body = self.request("GET", "/api/watchdog/status")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["path"], "/api/watchdog/status")

    def test_root_is_session_manager_home(self) -> None:
        status, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("Codex 会话管理".encode(), body)


class StatelessCommandTests(unittest.TestCase):
    def test_version_does_not_create_runtime_directories(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "app.py"),
                    "--version",
                    "--mode",
                    "installed",
                    "--data-dir",
                    str(home / "data"),
                    "--runtime-dir",
                    str(home / "runtime"),
                    "--log-dir",
                    str(home / "logs"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0.1.0")
            self.assertFalse(home.exists())


if __name__ == "__main__":
    unittest.main()
