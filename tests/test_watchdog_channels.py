from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from watchdog.channels import ChannelConfig, classify_http_status, probe_channel


class ProbeHandler(BaseHTTPRequestHandler):
    status = 200
    body = {"id": "chatcmpl-test", "choices": [{"message": {"content": "ok"}}]}
    received_authorization = ""
    received_payload: dict = {}

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        type(self).received_authorization = self.headers.get("Authorization", "")
        length = int(self.headers.get("Content-Length", "0"))
        type(self).received_payload = json.loads(self.rfile.read(length))
        payload = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ChannelProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        ProbeHandler.status = 200
        ProbeHandler.body = {"id": "chatcmpl-test", "choices": [{"message": {"content": "ok"}}]}
        ProbeHandler.received_authorization = ""

    def test_healthy_probe_calls_real_chat_completion_endpoint(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_channel(ChannelConfig(
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                probe_url_override="",
                model="test-model",
                api_key="sk-secret",
                timeout_seconds=2.0,
            ))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(result.category, "healthy")
        self.assertEqual(result.http_status, 200)
        self.assertTrue(result.healthy)
        self.assertEqual(ProbeHandler.received_authorization, "Bearer sk-secret")
        self.assertEqual(ProbeHandler.received_payload["model"], "test-model")
        self.assertEqual(ProbeHandler.received_payload["max_tokens"], 1)

    def test_http_statuses_are_classified(self) -> None:
        expected = {401: "auth_error", 403: "auth_error", 429: "rate_limited", 502: "upstream_error", 503: "upstream_error", 504: "upstream_error", 418: "other_http_error"}
        for status, category in expected.items():
            with self.subTest(status=status):
                self.assertEqual(classify_http_status(status), category)

    def test_invalid_json_is_protocol_error_without_secret(self) -> None:
        ProbeHandler.body = {"unexpected": True}
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            result = probe_channel(ChannelConfig(f"http://127.0.0.1:{server.server_address[1]}", "", "model", "sk-secret", 2.0))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(result.category, "protocol_error")
        self.assertFalse(result.healthy)
        self.assertNotIn("sk-secret", result.detail)

    def test_connection_error_is_network_error(self) -> None:
        result = probe_channel(ChannelConfig("http://127.0.0.1:1", "", "model", "sk-secret", 0.1))
        self.assertEqual(result.category, "network_error")
        self.assertFalse(result.healthy)
        self.assertNotIn("sk-secret", result.detail)


if __name__ == "__main__":
    unittest.main()
