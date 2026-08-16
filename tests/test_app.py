import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from app import (
    DEFAULT_PROJECTS,
    Handler,
    WEBSITE_CACHE,
    cached_website_health,
    load_projects,
    probe_website,
    project_summary,
    project_url_is_valid,
    reorder_projects,
    running_pids,
    save_projects,
    state_for,
    states_for,
)


class ProjectConfigPortabilityTests(unittest.TestCase):
    def test_default_catalog_is_device_independent(self) -> None:
        self.assertEqual(DEFAULT_PROJECTS, [])

    def test_missing_local_config_starts_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "projects.json"
            with patch("app.CONFIG_PATH", config_path):
                self.assertEqual(load_projects(), [])

            self.assertEqual(config_path.read_text(encoding="utf-8"), "[]")

    def test_project_catalog_is_replaced_atomically_for_auxiliary_readers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "projects.json"
            with patch("app.CONFIG_PATH", config_path):
                save_projects([{"id": "project-a"}])

            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8")),
                [{"id": "project-a"}],
            )
            self.assertFalse(config_path.with_suffix(".json.tmp").exists())


class ProjectUrlValidationTests(unittest.TestCase):
    def test_external_projects_allow_http_or_https(self) -> None:
        self.assertTrue(project_url_is_valid("https://pt.example.com", external=True))
        self.assertTrue(project_url_is_valid("http://media.example.com", external=True))
        self.assertFalse(project_url_is_valid("", external=True))

    def test_credentials_in_urls_are_rejected(self) -> None:
        self.assertFalse(
            project_url_is_valid("https://admin:secret@pt.example.com", external=True)
        )

    def test_local_projects_allow_empty_http_or_https_urls(self) -> None:
        self.assertTrue(project_url_is_valid("", external=False))
        self.assertTrue(project_url_is_valid("http://127.0.0.1:8765", external=False))
        self.assertTrue(project_url_is_valid("https://localhost", external=False))
        self.assertFalse(project_url_is_valid("javascript:alert(1)", external=False))


class ProjectReorderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projects = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

    def test_complete_order_is_applied_in_place(self) -> None:
        self.assertTrue(reorder_projects(self.projects, ["c", "a", "b"]))
        self.assertEqual([project["id"] for project in self.projects], ["c", "a", "b"])

    def test_partial_or_duplicate_order_is_rejected_without_mutation(self) -> None:
        for ordered_ids in (["a", "b"], ["a", "a", "c"], "a,b,c"):
            with self.subTest(ordered_ids=ordered_ids):
                self.assertFalse(reorder_projects(self.projects, ordered_ids))
                self.assertEqual([project["id"] for project in self.projects], ["a", "b", "c"])


class ProjectSummaryTests(unittest.TestCase):
    def test_summary_exposes_counts_without_project_configuration(self) -> None:
        summary = project_summary(
            [
                {"mode": "local", "state": "running", "path": "private-a"},
                {"mode": "local", "state": "stopped", "path": "private-b"},
                {"mode": "external", "state": "online", "url": "https://private"},
            ]
        )

        self.assertEqual(summary["runningCount"], 1)
        self.assertEqual(summary["localCount"], 2)
        self.assertEqual(
            summary["counts"],
            {"all": 3, "running": 1, "stopped": 1, "needs-config": 0, "external": 1},
        )
        self.assertNotIn("projects", summary)


class ProjectStateBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        WEBSITE_CACHE.clear()

    def test_slow_external_probe_does_not_block_project_state_response(self) -> None:
        probe_started = threading.Event()
        release_probe = threading.Event()
        state_ready = threading.Event()
        result: list[dict] = []

        def slow_probe(_url: object) -> dict:
            probe_started.set()
            release_probe.wait(timeout=2)
            return {
                "online": True,
                "status": 200,
                "detail": "HTTP 200",
                "checkedAt": "2026-08-05 12:00:00",
            }

        def collect_states() -> None:
            result.extend(
                states_for(
                    [
                        {
                            "id": "external-project",
                            "name": "Slow website",
                            "mode": "external",
                            "url": "https://slow.example.com",
                            "path": "",
                            "startCommand": "",
                            "port": "",
                            "pid": None,
                        }
                    ]
                )
            )
            state_ready.set()

        with patch("app.probe_website", side_effect=slow_probe):
            worker = threading.Thread(target=collect_states)
            worker.start()
            try:
                self.assertTrue(probe_started.wait(timeout=1))
                returned_while_probe_is_running = state_ready.wait(timeout=0.25)
            finally:
                release_probe.set()
                worker.join(timeout=2)

        self.assertTrue(returned_while_probe_is_running)
        self.assertEqual(result[0]["websiteDetail"], "检测中")

    def test_single_managed_process_uses_targeted_pid_check(self) -> None:
        with patch("app.pid_is_running", return_value=True) as check:
            active = running_pids([None, "2048", ""])

        self.assertEqual(active, {2048})
        check.assert_called_once_with(2048)

    def test_multiple_managed_processes_use_only_targeted_pid_checks(self) -> None:
        with patch("app.pid_is_running", side_effect=lambda pid: pid == 2048) as check:
            active = running_pids(["2048", "4096", "2048"])

        self.assertEqual(active, {2048})
        self.assertEqual({call.args[0] for call in check.call_args_list}, {2048, 4096})


class HandlerConnectionTests(unittest.TestCase):
    def test_client_disconnects_are_silent_and_close_the_connection(self) -> None:
        for error in (
            BrokenPipeError(),
            ConnectionAbortedError(),
            ConnectionResetError(),
        ):
            with self.subTest(error=type(error).__name__):
                handler = Handler.__new__(Handler)
                handler.close_connection = False
                with patch.object(
                    BaseHTTPRequestHandler,
                    "handle_one_request",
                    side_effect=error,
                ):
                    handler.handle_one_request()
                self.assertTrue(handler.close_connection)

    def test_unexpected_request_errors_still_propagate(self) -> None:
        handler = Handler.__new__(Handler)
        with (
            patch.object(
                BaseHTTPRequestHandler,
                "handle_one_request",
                side_effect=RuntimeError("unexpected"),
            ),
            self.assertRaisesRegex(RuntimeError, "unexpected"),
        ):
            handler.handle_one_request()


class WebsiteProbeTests(unittest.TestCase):
    def test_successful_response_is_online(self) -> None:
        response = MagicMock()
        response.getcode.return_value = 200
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response

        with patch("app.build_opener", return_value=opener):
            health = probe_website("https://example.com")

        self.assertTrue(health["online"])
        self.assertEqual(health["status"], 200)

    def test_authentication_response_still_proves_site_is_online(self) -> None:
        error = HTTPError("https://example.com", 403, "Forbidden", {}, None)
        opener = MagicMock()
        opener.open.side_effect = error

        with patch("app.build_opener", return_value=opener):
            health = probe_website("https://example.com")

        self.assertTrue(health["online"])
        self.assertEqual(health["status"], 403)

    def test_missing_or_server_error_response_is_offline(self) -> None:
        for status in (404, 410, 503):
            with self.subTest(status=status):
                error = HTTPError("https://example.com", status, "Unavailable", {}, None)
                opener = MagicMock()
                opener.open.side_effect = error
                with patch("app.build_opener", return_value=opener):
                    health = probe_website("https://example.com")
                self.assertFalse(health["online"])
                self.assertEqual(health["status"], status)

    def test_redirect_response_is_online_without_following_target(self) -> None:
        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/unreachable")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            health = probe_website(
                f"http://127.0.0.1:{server.server_address[1]}/protected"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertTrue(health["online"])
        self.assertEqual(health["status"], 302)

    def test_open_web_port_is_online_when_http_connection_fails(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = TimeoutError()
        connection = MagicMock()
        connection.__enter__.return_value = connection

        with (
            patch("app.build_opener", return_value=opener),
            patch("app.socket.create_connection", return_value=connection),
        ):
            health = probe_website("https://example.com/dashboard")

        self.assertTrue(health["online"])
        self.assertIsNone(health["status"])
        self.assertIn("443", health["detail"])
        self.assertIn("HTTP 复检中", health["detail"])

    def test_closed_web_port_is_offline_after_http_connection_fails(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = TimeoutError()

        with (
            patch("app.build_opener", return_value=opener),
            patch("app.socket.create_connection", side_effect=OSError()),
        ):
            health = probe_website("https://example.com/dashboard")

        self.assertFalse(health["online"])
        self.assertEqual(health["detail"], "连接失败")


class WebsiteHealthCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        WEBSITE_CACHE.clear()

    def tearDown(self) -> None:
        WEBSITE_CACHE.clear()

    def test_transient_failures_keep_last_success_until_threshold(self) -> None:
        online = {
            "online": True,
            "status": 200,
            "detail": "HTTP 200",
            "checkedAt": "08:00:00",
        }
        offline = {
            "online": False,
            "status": None,
            "detail": "连接失败",
            "checkedAt": "08:00:15",
        }

        with (
            patch("app.probe_website", side_effect=[online, offline, offline, offline]),
            patch("app.time.monotonic", side_effect=[0.0, 16.0, 32.0, 48.0]),
        ):
            first = cached_website_health("https://example.com")
            second = cached_website_health("https://example.com")
            third = cached_website_health("https://example.com")
            fourth = cached_website_health("https://example.com")

        self.assertTrue(first["online"])
        self.assertTrue(second["online"])
        self.assertTrue(third["online"])
        self.assertIn("复检中", second["detail"])
        self.assertFalse(fourth["online"])

class ExternalProjectStateTests(unittest.TestCase):
    project = {
        "id": "remote-tool",
        "mode": "external",
        "name": "Remote tool",
        "url": "https://example.com",
        "path": "",
        "startCommand": "",
        "port": "",
        "pid": None,
    }

    def test_online_external_project_is_reported_online(self) -> None:
        result = state_for(
            self.project,
            website_health={"online": True, "status": 200, "detail": "HTTP 200"},
        )

        self.assertEqual(result["state"], "online")
        self.assertEqual(result["stateLabel"], "在线")
        self.assertTrue(result["websiteActive"])

    def test_unreachable_external_project_is_reported_offline(self) -> None:
        result = state_for(
            self.project,
            website_health={"online": False, "status": None, "detail": "连接失败"},
        )

        self.assertEqual(result["state"], "offline")
        self.assertEqual(result["stateLabel"], "离线")
        self.assertFalse(result["websiteActive"])


if __name__ == "__main__":
    unittest.main()
