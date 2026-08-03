import unittest

from watchdog.http_api import WatchdogHttpApi


class FakeService:
    def __init__(self) -> None:
        self.check_calls: list[str] = []

    def create_channel(self, payload: dict) -> dict:
        return {
            "id": "channel-1",
            "name": payload["name"],
            "apiKeyMasked": "已保存",
        }

    def get_status(self) -> dict:
        return {"schedulerRunning": True}

    def get_settings(self) -> dict:
        return {"defaultIntervalMinutes": 15}

    def update_settings(self, payload: dict) -> dict:
        return payload

    def list_channels(self) -> list[dict]:
        return []

    def list_recovery_rules(self) -> list[dict]:
        return [{"id": "http-503", "builtIn": True}]

    def get_recovery_rule(self, rule_id: str) -> dict:
        return {"id": rule_id}

    def create_recovery_rule(self, payload: dict) -> dict:
        return {"id": "rule-1", **payload}

    def update_recovery_rule(self, rule_id: str, payload: dict) -> dict:
        return {"id": rule_id, **payload}

    def delete_recovery_rule(self, rule_id: str) -> None:
        return None

    def get_channel(self, channel_id: str) -> dict:
        return {"id": channel_id}

    def update_channel(self, channel_id: str, payload: dict) -> dict:
        return {"id": channel_id, **payload}

    def delete_channel(self, channel_id: str) -> None:
        return None

    def probe_channel(self, channel_id: str) -> dict:
        return {"category": "healthy"}

    def list_sessions(self) -> list[dict]:
        return []

    def get_session(self, session_id: str) -> dict:
        return {"id": session_id}

    def create_session(self, payload: dict) -> dict:
        return {"id": "session-1", **payload}

    def update_session(self, session_id: str, payload: dict) -> dict:
        return {"id": session_id, **payload}

    def delete_session(self, session_id: str) -> None:
        return None

    def check_session(self, session_id: str) -> dict:
        self.check_calls.append(session_id)
        return {"decision": "silent_session_running"}

    def list_local_sessions(self, limit: int) -> list[dict]:
        return [{"threadId": "thread-1", "limit": limit}]

    def list_runs(self, filters: dict) -> list[dict]:
        return [{"decision": filters.get("decision", "silent")}]

    def get_desktop_bridge_status(self) -> dict:
        return {"pending": 1}

    def claim_desktop_bridge_job(self, payload: dict) -> dict:
        return {"id": "job-1", "runnerId": payload["runnerId"]}

    def mark_desktop_bridge_started(self, job_id: str, payload: dict) -> dict:
        return {"id": job_id, "decision": "resume_started", **payload}

    def finish_desktop_bridge_job(self, job_id: str, payload: dict) -> dict:
        return {"id": job_id, "decision": "resume_completed", **payload}


class WatchdogApiTests(unittest.TestCase):
    def test_channel_create_never_returns_plaintext_key(self) -> None:
        api = WatchdogHttpApi(FakeService())

        response = api.dispatch(
            "POST",
            "/api/watchdog/channels",
            {},
            {
                "name": "主兼容渠道",
                "baseUrl": "https://api.example/v1",
                "model": "model-a",
                "apiKey": "sk-secret",
            },
        )

        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["apiKeyMasked"], "已保存")
        self.assertNotIn("sk-secret", repr(response.body))

    def test_manual_check_uses_normal_safety_pipeline(self) -> None:
        service = FakeService()
        api = WatchdogHttpApi(service)

        response = api.dispatch(
            "POST", "/api/watchdog/sessions/session-1/check", {}, {}
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(service.check_calls, ["session-1"])

    def test_project_routes_are_not_claimed(self) -> None:
        api = WatchdogHttpApi(FakeService())
        self.assertIsNone(api.dispatch("GET", "/api/projects", {}, None))

    def test_required_watchdog_routes_are_dispatched(self) -> None:
        api = WatchdogHttpApi(FakeService())
        routes = [
            ("GET", "/api/watchdog/status", {}, None, 200),
            ("GET", "/api/watchdog/settings", {}, None, 200),
            ("PUT", "/api/watchdog/settings", {}, {"schedulerEnabled": True}, 200),
            ("GET", "/api/watchdog/channels", {}, None, 200),
            ("GET", "/api/watchdog/recovery-rules", {}, None, 200),
            (
                "POST",
                "/api/watchdog/recovery-rules",
                {},
                {"name": "capacity", "pattern": "model is at capacity"},
                201,
            ),
            ("GET", "/api/watchdog/recovery-rules/rule-1", {}, None, 200),
            (
                "PUT",
                "/api/watchdog/recovery-rules/rule-1",
                {},
                {"enabled": False},
                200,
            ),
            ("DELETE", "/api/watchdog/recovery-rules/rule-1", {}, None, 204),
            ("GET", "/api/watchdog/channels/channel-1", {}, None, 200),
            ("PUT", "/api/watchdog/channels/channel-1", {}, {"name": "backup"}, 200),
            ("DELETE", "/api/watchdog/channels/channel-1", {}, None, 204),
            ("POST", "/api/watchdog/channels/channel-1/probe", {}, {}, 200),
            ("GET", "/api/watchdog/sessions", {}, None, 200),
            ("POST", "/api/watchdog/sessions", {}, {"name": "sample-development-session"}, 201),
            ("GET", "/api/watchdog/sessions/session-1", {}, None, 200),
            ("PUT", "/api/watchdog/sessions/session-1", {}, {"name": "sample-development-session"}, 200),
            ("DELETE", "/api/watchdog/sessions/session-1", {}, None, 204),
            ("GET", "/api/watchdog/local-codex-sessions", {"limit": ["20"]}, None, 200),
            ("GET", "/api/watchdog/runs", {"decision": ["silent"]}, None, 200),
            ("GET", "/api/watchdog/bridge/status", {}, None, 200),
            (
                "POST",
                "/api/watchdog/bridge/jobs/claim",
                {},
                {"runnerId": "desktop-test"},
                200,
            ),
            (
                "POST",
                "/api/watchdog/bridge/jobs/job-1/started",
                {},
                {"leaseToken": "lease-1", "resumedTurnId": "turn-1"},
                200,
            ),
            (
                "POST",
                "/api/watchdog/bridge/jobs/job-1/finish",
                {},
                {"leaseToken": "lease-1", "outcome": "completed"},
                200,
            ),
        ]
        for method, path, query, payload, expected in routes:
            with self.subTest(method=method, path=path):
                response = api.dispatch(method, path, query, payload)
                self.assertEqual(response.status, expected)


if __name__ == "__main__":
    unittest.main()
