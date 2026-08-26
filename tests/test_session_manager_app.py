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
from types import SimpleNamespace
from unittest.mock import patch

import app
from watchdog.router import ApiResponse


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
        with patch.object(app, "STARTUP_STATE", {"phase": "ready", "error": ""}):
            status, body = self.request("GET", "/api/health")
            payload = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "codex-session-manager")
        self.assertEqual(payload["role"], "session-manager")
        self.assertEqual(payload["ports"], {"admin": 8767, "remote": 8766})

    def test_diagnostics_reports_memory_cache_tunnel_and_codex(self) -> None:
        class DiagnosticRuntime:
            projection = SimpleNamespace(stats=lambda: {"sessions": 3, "maxSessions": 24})
            event_hub = SimpleNamespace(
                stats=lambda: {"sequence": 9, "bufferedEvents": 4}
            )
            tunnel = SimpleNamespace(
                status=lambda: {
                    "running": True,
                    "restartCount": 1,
                    "recentRestarts": [{"reason": "process-exit"}],
                }
            )

        class DiagnosticCodex:
            @staticmethod
            def status():
                return {"connected": True, "connecting": False}

        with (
            patch.object(app, "STARTUP_STATE", {"phase": "ready", "error": ""}),
            patch.object(app, "SESSION_RUNTIME", DiagnosticRuntime()),
            patch.object(app, "CODEX_RUNTIME", DiagnosticCodex()),
        ):
            status, body = self.request("GET", "/api/diagnostics")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        self.assertGreater(payload["process"]["rssBytes"], 0)
        self.assertEqual(payload["projection"]["sessions"], 3)
        self.assertEqual(payload["eventHub"]["sequence"], 9)
        self.assertEqual(payload["tunnel"]["restartCount"], 1)
        self.assertTrue(payload["codex"]["connected"])

    def test_health_responds_while_runtime_is_starting(self) -> None:
        with patch.object(app, "STARTUP_STATE", {"phase": "starting", "error": ""}):
            status, body = self.request("GET", "/api/health")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["startup"]["phase"], "starting")

    def test_health_reports_runtime_initialization_failure(self) -> None:
        startup = {"phase": "failed", "error": "database could not be opened"}
        with patch.object(app, "STARTUP_STATE", startup):
            status, body = self.request("GET", "/api/health")

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(payload["ready"])
        self.assertEqual(payload["startup"], startup)

    def test_runtime_api_returns_service_unavailable_while_starting(self) -> None:
        with patch.object(app, "WATCHDOG_API", None), patch.object(
            app, "STARTUP_STATE", {"phase": "starting", "error": ""}
        ):
            status, body = self.request("GET", "/api/watchdog/status")

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["startup"]["phase"], "starting")

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
    def test_importing_adapter_does_not_import_watchdog_router(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import watchdog.codex_adapter; "
                    "print('watchdog.router' in sys.modules); "
                    "print('watchdog.application' in sys.modules)"
                ),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["False", "False"])

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
