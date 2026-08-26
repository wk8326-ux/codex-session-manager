import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from watchdog.application import WatchdogApplication
from watchdog.channels import ProbeResult
from watchdog.router import WatchdogHttpApi
from watchdog.models import SessionSnapshot
from watchdog.store import WatchdogStore


class FakeSecretStore:
    def __init__(self) -> None:
        self.protected: list[str] = []

    def protect(self, value: str) -> bytes:
        self.protected.append(value)
        return ("encrypted:" + value).encode("utf-8")

    def unprotect(self, value: bytes) -> str:
        return value.decode("utf-8").removeprefix("encrypted:")


class FakeAdapter:
    def list_threads(self, limit: int) -> list[SessionSnapshot]:
        return [
            SessionSnapshot(
                thread_id="11111111-1111-1111-1111-111111111111",
                name="sample-development-session",
                thread_status="idle",
            )
        ][:limit]


class FakeMonitorService:
    def check_session(self, session_id: str, now: str) -> dict:
        return {
            "id": "run-1",
            "sessionId": session_id,
            "decision": "silent_session_running",
            "detailSanitized": "session is running",
            "startedAt": now,
        }


class FakeScheduler:
    is_running = True


class WatchdogApplicationApiTests(unittest.TestCase):
    def test_user_can_configure_safe_channel_and_session_resources(self) -> None:
        with TemporaryDirectory() as directory:
            store = WatchdogStore(Path(directory) / "watchdog.db")
            store.initialize()
            secrets = FakeSecretStore()
            now = "2026-07-31T14:00:00Z"
            application = WatchdogApplication(
                store,
                secrets,
                lambda _config: ProbeResult("healthy", 200, "ok", 12, now),
                FakeAdapter(),
                FakeMonitorService(),
                FakeScheduler(),
                now_provider=lambda: now,
            )
            api = WatchdogHttpApi(application)

            channel_response = api.dispatch(
                "POST",
                "/api/watchdog/channels",
                {},
                {
                    "name": "primary",
                    "baseUrl": "https://api.example/v1",
                    "model": "model-a",
                    "apiKey": "sk-secret",
                },
            )
            self.assertEqual(channel_response.status, 201)
            self.assertNotIn("encryptedKey", channel_response.body)
            self.assertNotIn("sk-secret", repr(channel_response.body))
            channel_id = channel_response.body["id"]

            update_response = api.dispatch(
                "PUT",
                f"/api/watchdog/channels/{channel_id}",
                {},
                {"apiKey": "", "name": "primary updated"},
            )
            self.assertEqual(update_response.status, 200)
            self.assertEqual(secrets.protected, ["sk-secret"])

            session_response = api.dispatch(
                "POST",
                "/api/watchdog/sessions",
                {},
                {
                    "name": "sample-development-session",
                    "threadId": "11111111-1111-1111-1111-111111111111",
                    "channelId": channel_id,
                },
            )
            self.assertEqual(session_response.status, 201)
            self.assertEqual(session_response.body["effectiveIntervalMinutes"], 15)
            self.assertEqual(session_response.body["nextCheckAt"], now)
            self.assertFalse(
                session_response.body["unattendedApprovalsEnabled"]
            )

            updated_session = api.dispatch(
                "PUT",
                f"/api/watchdog/sessions/{session_response.body['id']}",
                {},
                {"unattendedApprovalsEnabled": True},
            )
            self.assertEqual(updated_session.status, 200)
            self.assertTrue(
                updated_session.body["unattendedApprovalsEnabled"]
            )

            status = api.dispatch("GET", "/api/watchdog/status", {}, None)
            self.assertTrue(status.body["schedulerRunning"])
            self.assertEqual(status.body["nextCheckAt"], now)

            bridge_settings = api.dispatch(
                "PUT",
                "/api/watchdog/settings",
                {},
                {"resumeDispatchMode": "desktop_bridge"},
            )
            self.assertEqual(
                bridge_settings.body["resumeDispatchMode"], "desktop_bridge"
            )
            invalid_mode = api.dispatch(
                "PUT",
                "/api/watchdog/settings",
                {},
                {"resumeDispatchMode": "both"},
            )
            self.assertEqual(invalid_mode.status, 400)

            created_rule = api.dispatch(
                "POST",
                "/api/watchdog/recovery-rules",
                {},
                {
                    "name": "Model capacity",
                    "pattern": "Selected model is at capacity",
                },
            )
            self.assertEqual(created_rule.status, 201)
            self.assertEqual(created_rule.body["matchType"], "message_contains")
            self.assertFalse(created_rule.body["builtIn"])

            rule_id = created_rule.body["id"]
            updated_rule = api.dispatch(
                "PUT",
                f"/api/watchdog/recovery-rules/{rule_id}",
                {},
                {"enabled": False},
            )
            self.assertEqual(updated_rule.status, 200)
            self.assertFalse(updated_rule.body["enabled"])
            self.assertEqual(
                api.dispatch(
                    "DELETE",
                    f"/api/watchdog/recovery-rules/{rule_id}",
                    {},
                    None,
                ).status,
                204,
            )
            self.assertEqual(
                api.dispatch(
                    "DELETE",
                    "/api/watchdog/recovery-rules/http-503",
                    {},
                    None,
                ).status,
                409,
            )

    def test_dashboard_resources_have_stable_ui_response_shapes(self) -> None:
        with TemporaryDirectory() as directory:
            store = WatchdogStore(Path(directory) / "watchdog.db")
            store.initialize()
            secrets = FakeSecretStore()
            now = "2026-07-31T14:00:00Z"
            application = WatchdogApplication(
                store,
                secrets,
                lambda _config: ProbeResult("healthy", 200, "ok", 12, now),
                FakeAdapter(),
                FakeMonitorService(),
                FakeScheduler(),
                now_provider=lambda: now,
            )
            api = WatchdogHttpApi(application)
            channel = api.dispatch(
                "POST",
                "/api/watchdog/channels",
                {},
                {
                    "name": "primary",
                    "baseUrl": "https://api.example/v1",
                    "model": "model-a",
                    "apiKey": "sk-secret",
                },
            ).body
            api.dispatch(
                "POST",
                f"/api/watchdog/channels/{channel['id']}/probe",
                {},
                {},
            )
            session = api.dispatch(
                "POST",
                "/api/watchdog/sessions",
                {},
                {
                    "name": "sample-development-session",
                    "threadId": "11111111-1111-1111-1111-111111111111",
                    "channelId": channel["id"],
                    "intervalMinutes": 10,
                },
            ).body
            store.set_next_check(
                session["id"],
                now,
                "2026-07-31T14:10:00Z",
                "inProgress",
                "silent_session_running",
                "turn-1",
            )
            store.create_monitor_run(
                {
                    "sessionId": session["id"],
                    "channelId": channel["id"],
                    "startedAt": now,
                    "finishedAt": now,
                    "decision": "silent_session_running",
                    "detailSanitized": "session is running",
                }
            )

            status = api.dispatch("GET", "/api/watchdog/status", {}, None).body
            sessions = api.dispatch("GET", "/api/watchdog/sessions", {}, None).body
            channels = api.dispatch("GET", "/api/watchdog/channels", {}, None).body
            runs = api.dispatch("GET", "/api/watchdog/runs", {}, None).body

            self.assertEqual(
                set(status),
                {
                    "schedulerRunning",
                    "schedulerEnabled",
                    "codexConnected",
                    "resumeActionsEnabled",
                    "resumeDispatchMode",
                    "desktopBridge",
                    "nextCheckAt",
                },
            )
            self.assertEqual(sessions[0]["state"], "running")
            self.assertEqual(sessions[0]["effectiveIntervalMinutes"], 10)
            self.assertEqual(channels[0]["apiKeyMasked"], "已保存")
            self.assertEqual(channels[0]["lastProbeCategory"], "healthy")
            self.assertEqual(runs[0]["detail"], "session is running")


if __name__ == "__main__":
    unittest.main()
