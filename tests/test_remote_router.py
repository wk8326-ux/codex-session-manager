from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote.application import RemoteApplication
from remote.approvals import RemoteApprovalBroker
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
    def __init__(self) -> None:
        self.last_turn_limit: int | None = None

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        self.last_turn_limit = turn_limit
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
        self.adapter = Adapter()
        self.approval_decisions: list[str] = []
        self.approvals = RemoteApprovalBroker(
            lambda: {
                session["threadId"] for session in self.store.list_synced_sessions()
            },
            timeout_seconds=2,
            record_audit=self.store.record_approval_audit,
        )
        application = RemoteApplication(
            self.store,
            self.adapter,
            RemoteEventHub(lambda: {THREAD_ID}),
            default_base_url="http://192.0.2.10:8766",
            codex_connected=True,
            approval_broker=self.approvals,
        )
        self.native_capture_calls = 0

        def capture_region() -> dict:
            self.native_capture_calls += 1
            return {
                "captured": True,
                "image": "data:image/png;base64,iVBORw0KGgo=",
                "name": "region.png",
            }

        self.admin = AdminRemoteApi(application, native_capture=capture_region)
        self.remote = RemoteHttpApi(application)

    def tearDown(self) -> None:
        self.approvals.close()
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

    def test_session_summary_route_skips_expensive_status_enrichment(self) -> None:
        created = self.admin.dispatch(
            "POST",
            "/api/remote/synced-sessions",
            {"name": "Remote Woxsheet", "threadId": THREAD_ID},
        )
        token = self.pair()

        admin_list = self.admin.dispatch("GET", "/api/remote/sessions?summary=1", {})
        remote_list = self.remote.dispatch(
            "GET",
            "/api/remote/sessions?summary=true",
            {"Authorization": f"Bearer {token}"},
            None,
        )

        self.assertEqual(admin_list.status, 200)
        self.assertEqual(remote_list.status, 200)
        self.assertEqual(admin_list.body[0]["id"], created.body["id"])
        self.assertFalse(admin_list.body[0]["statusKnown"])
        self.assertEqual(remote_list.body, admin_list.body)

    def test_native_region_capture_is_available_only_on_the_local_admin_api(
        self,
    ) -> None:
        response = self.admin.dispatch("POST", "/api/remote/native-screenshot", {})

        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["captured"])
        self.assertEqual(self.native_capture_calls, 1)

        token = self.pair()
        remote_response = self.remote.dispatch(
            "POST",
            "/api/remote/native-screenshot",
            {"Authorization": f"Bearer {token}"},
            {},
        )
        self.assertIsNone(remote_response)
        self.assertEqual(self.native_capture_calls, 1)

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
        self.assertEqual(
            {key: listed.body[0][key] for key in created.body},
            created.body,
        )
        self.assertFalse(listed.body[0]["statusKnown"])

        deleted = self.admin.dispatch(
            "DELETE", f"/api/remote/synced-sessions/{created.body['id']}", None
        )
        self.assertEqual(deleted.status, 204)

    def test_session_detail_route_accepts_a_bounded_turn_limit(self) -> None:
        created = self.admin.dispatch(
            "POST",
            "/api/remote/synced-sessions",
            {"name": "Remote Woxsheet", "threadId": THREAD_ID},
        )

        response = self.admin.dispatch(
            "GET", f"/api/remote/sessions/{created.body['id']}?turnLimit=6", None
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(self.adapter.last_turn_limit, 6)

        invalid = self.admin.dispatch(
            "GET", f"/api/remote/sessions/{created.body['id']}?turnLimit=bad", None
        )
        self.assertEqual(invalid.status, 400)

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

    def test_authenticated_device_can_restore_its_persisted_identity(self) -> None:
        token = self.pair()
        response = self.remote.dispatch(
            "GET",
            "/api/remote/device",
            {"Authorization": f"Bearer {token}"},
            None,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["name"], "Phone")
        self.assertIn("createdAt", response.body)

    def test_authenticated_device_can_resolve_pending_approval(self) -> None:
        self.admin.dispatch(
            "POST",
            "/api/remote/synced-sessions",
            {"name": "Remote Woxsheet", "threadId": THREAD_ID},
        )
        self.approvals.offer(
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD_ID,
                "turnId": "turn-1",
                "command": "npm test",
                "availableDecisions": ["accept", "decline"],
            },
            self.approval_decisions.append,
        )
        token = self.pair()
        headers = {"Authorization": f"Bearer {token}"}

        listed = self.remote.dispatch("GET", "/api/remote/approvals", headers, None)
        self.assertEqual(listed.status, 200)
        self.assertEqual(listed.body[0]["summary"], "npm test")
        approval_id = listed.body[0]["id"]

        resolved = self.remote.dispatch(
            "POST",
            f"/api/remote/approvals/{approval_id}/decision",
            headers,
            {"decision": "accept"},
        )

        self.assertEqual(resolved.status, 200)
        self.assertEqual(self.approval_decisions, ["accept"])
        self.assertEqual(
            self.remote.dispatch("GET", "/api/remote/approvals", headers, None).body,
            [],
        )

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

    def test_remote_health_is_available_without_device_authentication(self) -> None:
        response = self.remote.dispatch("GET", "/api/remote/health", {}, None)

        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["ready"])


if __name__ == "__main__":
    unittest.main()
