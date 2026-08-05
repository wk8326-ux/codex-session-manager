from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote.application import RemoteApplication
from remote.events import RemoteEventHub
from remote.router import AdminRemoteApi, RemoteHttpApi
from remote.store import RemoteStore


THREAD_ID = "00000000-0000-4000-8000-000000000001"


class Sessions:
    def list_sessions(self) -> list[dict]:
        return [
            {
                "id": "session-1",
                "name": "Registered",
                "threadId": THREAD_ID,
                "enabled": True,
            }
        ]

    def get_session(self, session_id: str) -> dict | None:
        return self.list_sessions()[0] if session_id == "session-1" else None


class Adapter:
    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        return {"threadId": thread_id, "status": "idle", "turns": []}

    def send_message(self, thread_id: str, prompt: str) -> dict:
        return {"turnId": "turn-new", "delivery": "started"}

    def list_threads(self, limit: int) -> list[object]:
        return [
            SimpleNamespace(
                thread_id=THREAD_ID,
                name="Local session",
                thread_status="idle",
                active_flags=(),
            )
        ][:limit]


class RemoteRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = RemoteStore(Path(self.directory.name) / "watchdog.db")
        self.store.initialize()
        sessions = Sessions()
        application = RemoteApplication(
            self.store,
            Adapter(),
            RemoteEventHub(lambda: {THREAD_ID}),
            lambda: [],
            default_base_url="http://192.0.2.10:8766",
            codex_connected=True,
        )
        self.admin = AdminRemoteApi(application)
        self.remote = RemoteHttpApi(application)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def pair(self) -> str:
        created = self.admin.dispatch(
            "POST", "/api/remote/pairings", {"baseUrl": "http://192.0.2.10:8766"}
        )
        self.assertIsNotNone(created)
        self.assertIn("#secret=", created.body["pairingUrl"])
        self.assertNotIn("?secret=", created.body["pairingUrl"])
        claimed = self.remote.dispatch(
            "POST",
            f"/api/remote/pairings/{created.body['id']}/claim",
            {},
            {"secret": created.body["secret"], "deviceName": "Phone"},
        )
        self.assertIsNotNone(claimed)
        return claimed.body["deviceToken"]

    def test_remote_routes_require_a_device_token(self) -> None:
        response = self.remote.dispatch("GET", "/api/remote/sessions", {}, None)

        self.assertEqual(response.status, 401)

    def test_admin_can_select_local_session_for_remote_sync(self) -> None:
        local = self.admin.dispatch("GET", "/api/remote/local-sessions?limit=50", None)
        self.assertEqual(local.status, 200)
        self.assertEqual(local.body[0]["threadId"], THREAD_ID)

        created = self.admin.dispatch(
            "POST",
            "/api/remote/synced-sessions",
            {"name": "Remote Woxsheet", "threadId": THREAD_ID},
        )
        self.assertEqual(created.status, 201)

        listed = self.admin.dispatch("GET", "/api/remote/synced-sessions", None)
        self.assertEqual(listed.body, [created.body])

        deleted = self.admin.dispatch(
            "DELETE", f"/api/remote/synced-sessions/{created.body['id']}", None
        )
        self.assertEqual(deleted.status, 204)

    def test_authenticated_device_only_sees_synced_sessions(self) -> None:
        created = self.admin.dispatch(
            "POST",
            "/api/remote/synced-sessions",
            {"name": "Remote Woxsheet", "threadId": THREAD_ID},
        )
        token = self.pair()
        response = self.remote.dispatch(
            "GET",
            "/api/remote/sessions",
            {"Authorization": f"Bearer {token}"},
            None,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual([item["id"] for item in response.body], [created.body["id"]])
        self.assertNotIn("resumePrompt", response.body[0])

    def test_remote_router_has_no_admin_or_project_crud_routes(self) -> None:
        token = self.pair()
        headers = {"Authorization": f"Bearer {token}"}

        self.assertIsNone(
            self.remote.dispatch("GET", "/api/remote/devices", headers, None)
        )
        self.assertIsNone(
            self.remote.dispatch("POST", "/api/projects", headers, {"name": "bad"})
        )
        self.assertIsNone(
            self.remote.dispatch("GET", "/api/watchdog/channels", headers, None)
        )


if __name__ == "__main__":
    unittest.main()
