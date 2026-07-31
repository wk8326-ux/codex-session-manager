import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from watchdog.application import WatchdogApplication
from watchdog.channels import ProbeResult
from watchdog.http_api import WatchdogHttpApi
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
                name="woxsheet",
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
                    "name": "woxsheet",
                    "threadId": "11111111-1111-1111-1111-111111111111",
                    "channelId": channel_id,
                },
            )
            self.assertEqual(session_response.status, 201)
            self.assertEqual(session_response.body["effectiveIntervalMinutes"], 15)
            self.assertEqual(session_response.body["nextCheckAt"], now)

            status = api.dispatch("GET", "/api/watchdog/status", {}, None)
            self.assertTrue(status.body["schedulerRunning"])
            self.assertEqual(status.body["nextCheckAt"], now)


if __name__ == "__main__":
    unittest.main()
