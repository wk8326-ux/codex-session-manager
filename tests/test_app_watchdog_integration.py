import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from watchdog.http_api import ApiResponse


class FakeWatchdogApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def dispatch(self, method: str, path: str, query: dict, payload: object):
        self.calls.append((method, path))
        return ApiResponse(200, {"source": "watchdog"})


class HandlerWatchdogIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeWatchdogApi()
        self.api_patch = patch.object(app, "WATCHDOG_API", self.api, create=True)
        self.api_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.api_patch.stop()

    def request(self, method: str, path: str, payload: dict | None = None):
        connection = HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_project_routes_and_root_never_call_watchdog_dispatch(self) -> None:
        with patch.object(app, "states_for", return_value=[]):
            self.assertEqual(self.request("GET", "/api/projects")[0], 200)
        self.assertEqual(
            self.request("POST", "/api/projects/reorder", {"ids": []})[0], 400
        )
        self.assertEqual(
            self.request("PUT", "/api/projects/missing", {"name": "missing"})[0],
            404,
        )
        self.assertEqual(self.request("DELETE", "/api/projects/missing")[0], 404)
        status, root = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", root)
        self.assertEqual(self.api.calls, [])

    def test_watchdog_route_is_dispatched(self) -> None:
        status, body = self.request("GET", "/api/watchdog/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"source": "watchdog"})
        self.assertEqual(self.api.calls, [("GET", "/api/watchdog/status")])


if __name__ == "__main__":
    unittest.main()
