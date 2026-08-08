from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote.application import RemoteApplication, RemoteNotFound, RemoteValidationError
from remote.approvals import RemoteApprovalBroker
from remote.events import RemoteEventHub
from remote.store import RemoteStore


THREAD_ID = "00000000-0000-4000-8000-000000000001"


class SessionStore:
    def __init__(self) -> None:
        self.sessions = [
            {
                "id": "session-1",
                "name": "woxsheet",
                "threadId": THREAD_ID,
                "enabled": True,
                "lastSessionState": "active",
                "lastCheckResult": "silent_session_running",
                "lastCheckedAt": "2026-08-05T00:00:00Z",
            }
        ]

    def list_sessions(self) -> list[dict]:
        return list(self.sessions)

    def get_session(self, session_id: str) -> dict | None:
        return next((item for item in self.sessions if item["id"] == session_id), None)


class Adapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.last_turn_limit: int | None = None

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        self.last_turn_limit = turn_limit
        return {"threadId": thread_id, "status": "idle", "turns": []}

    def send_message(self, thread_id: str, prompt: str, image_url: str | None = None) -> dict:
        self.sent.append((thread_id, prompt, image_url))
        return {"turnId": "turn-2", "delivery": "started"}

    def list_threads(self, limit: int) -> list[object]:
        return [
            SimpleNamespace(
                thread_id=THREAD_ID,
                name="Local Woxsheet",
                thread_status="active",
                active_flags=("waiting",),
                latest_turn=SimpleNamespace(
                    status="failed",
                    error_message="unexpected status 503 Service Unavailable",
                    http_status=503,
                ),
            )
        ][:limit]


class RemoteApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        remote_store = RemoteStore(Path(self.temporary_directory.name) / "watchdog.db")
        remote_store.initialize()
        self.remote_store = remote_store
        self.sessions = SessionStore()
        self.adapter = Adapter()
        self.hub = RemoteEventHub(lambda: {THREAD_ID})
        self.approval_decisions: list[str] = []
        self.approvals = RemoteApprovalBroker(
            lambda: {
                session["threadId"]
                for session in self.remote_store.list_synced_sessions()
            },
            timeout_seconds=2,
            record_audit=self.remote_store.record_approval_audit,
        )
        self.application = RemoteApplication(
            remote_store,
            self.adapter,
            self.hub,
            lambda: [
                {
                    "id": "project-1",
                    "name": "Invoice",
                    "mode": "local",
                    "state": "running",
                    "stateLabel": "运行中",
                    "path": "C:\\private",
                    "startCommand": "secret command",
                }
            ],
            default_base_url="http://192.0.2.10:8766",
            codex_connected=True,
            approval_broker=self.approvals,
        )

    def tearDown(self) -> None:
        self.approvals.close()
        self.temporary_directory.cleanup()

    def test_message_is_sent_to_the_exact_registered_thread(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        response = self.application.send_message(synced["id"], {"message": "继续"})

        self.assertEqual(response["threadId"], THREAD_ID)
        self.assertEqual(self.adapter.sent, [(THREAD_ID, "继续", None)])
        with self.assertRaises(RemoteNotFound):
            self.application.send_message("unknown", {"message": "continue"})

    def test_message_accepts_one_valid_screenshot_and_rejects_invalid_data(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        image = "data:image/png;base64,iVBORw0KGgo="

        self.application.send_message(
            synced["id"], {"message": "请看截图", "image": image}
        )

        self.assertEqual(self.adapter.sent[-1], (THREAD_ID, "请看截图", image))
        with self.assertRaises(RemoteValidationError):
            self.application.send_message(
                synced["id"],
                {"message": "bad", "image": "data:text/html;base64,PGgxPmJhZDwvaDE+"},
            )

    def test_screenshot_can_be_sent_without_text(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        image = "data:image/jpeg;base64,/9j/2Q=="

        self.application.send_message(synced["id"], {"message": "", "image": image})

        self.assertEqual(self.adapter.sent[-1], (THREAD_ID, "", image))

    def test_admin_status_includes_local_tunnel_state(self) -> None:
        self.application.tunnel_status_provider = lambda: {
            "provider": "frp",
            "configured": True,
            "running": True,
            "state": "running",
            "pid": 1234,
            "startedAt": "2026-08-05T00:00:00Z",
            "detail": "",
        }

        status = self.application.admin_status()

        self.assertTrue(status["tunnel"]["running"])
        self.assertEqual(status["tunnel"]["provider"], "frp")
        self.assertEqual(
            status["projectSummary"], {"runningCount": 1, "localCount": 1}
        )

    def test_session_detail_is_limited_to_recent_turns(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        detail = self.application.read_session(synced["id"])

        self.assertEqual(detail["threadId"], THREAD_ID)
        self.assertEqual(self.adapter.last_turn_limit, 12)

        self.application.read_session(synced["id"], turn_limit=6)
        self.assertEqual(self.adapter.last_turn_limit, 6)

    def test_local_sessions_can_be_selected_without_entering_monitoring_catalog(self) -> None:
        local = self.application.list_local_sessions(limit=50)

        self.assertEqual(local[0]["threadId"], THREAD_ID)
        self.assertEqual(local[0]["threadStatus"], "active")
        self.assertEqual(self.sessions.list_sessions()[0]["name"], "woxsheet")
        self.assertEqual(self.application.list_sessions(), [])

        synced = self.application.create_synced_session(
            {"name": local[0]["name"], "threadId": local[0]["threadId"]}
        )
        listed = self.application.list_sessions()
        self.assertEqual(
            {key: listed[0][key] for key in synced},
            synced,
        )
        self.assertEqual(listed[0]["threadStatus"], "active")
        self.assertEqual(listed[0]["activeFlags"], ["waiting"])
        self.assertEqual(listed[0]["latestTurnStatus"], "failed")
        self.assertTrue(listed[0]["latestTurnHasError"])
        self.assertEqual(listed[0]["latestTurnHttpStatus"], 503)

        self.application.delete_synced_session(synced["id"])
        self.assertEqual(self.application.list_sessions(), [])

    def test_duplicate_synced_thread_is_rejected(self) -> None:
        payload = {"name": "Woxsheet", "threadId": THREAD_ID}
        self.application.create_synced_session(payload)

        with self.assertRaises(RemoteValidationError):
            self.application.create_synced_session(payload)

    def test_synced_session_exposes_and_resolves_remote_approval(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        self.assertTrue(
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
        )

        pending = self.application.list_approvals()[0]
        self.assertEqual(pending["sessionId"], synced["id"])
        self.assertEqual(pending["sessionName"], "Woxsheet")
        result = self.application.resolve_approval(
            pending["id"],
            {"decision": "accept"},
            {"id": "phone-1", "name": "Phone"},
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.approval_decisions, ["accept"])
        self.assertEqual(self.application.list_approvals(), [])
        self.assertEqual(
            self.remote_store.list_approval_audit()[0]["actorDeviceName"],
            "Phone",
        )

    def test_synced_session_can_allow_the_rest_of_the_current_turn(self) -> None:
        self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        self.approvals.offer(
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD_ID,
                "turnId": "turn-1",
                "command": "npm test",
                "availableDecisions": ["accept", "acceptForSession", "decline"],
            },
            self.approval_decisions.append,
        )
        pending = self.application.list_approvals()[0]

        result = self.application.resolve_approval(
            pending["id"],
            {"decision": "acceptForTurn"},
            {"id": "phone-1", "name": "Phone"},
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.approval_decisions, ["acceptForSession"])

    def test_removing_synced_session_declines_its_pending_approval(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        self.approvals.offer(
            "item/fileChange/requestApproval",
            {"threadId": THREAD_ID, "reason": "Update file"},
            self.approval_decisions.append,
        )

        self.application.delete_synced_session(synced["id"])

        self.assertEqual(self.approval_decisions, ["decline"])
        self.assertEqual(self.application.list_approvals(), [])

    def test_project_overview_omits_paths_commands_and_urls(self) -> None:
        projects = self.application.list_projects()

        self.assertEqual(projects[0]["state"], "running")
        self.assertNotIn("path", projects[0])
        self.assertNotIn("startCommand", projects[0])

    def test_event_hub_only_forwards_registered_thread_metadata(self) -> None:
        self.hub.publish(
            "turn/completed",
            {"threadId": THREAD_ID, "turn": {"id": "turn-1", "status": "completed"}},
        )
        self.hub.publish(
            "item/agentMessage/delta",
            {"threadId": "unregistered", "delta": "private text"},
        )

        batch = self.hub.wait(0, 0)

        self.assertEqual(len(batch["events"]), 1)
        self.assertEqual(batch["events"][0]["threadId"], THREAD_ID)
        self.assertNotIn("delta", batch["events"][0])

    def test_event_hub_exposes_safe_activity_metadata_without_command_content(self) -> None:
        self.hub.publish(
            "item/started",
            {
                "threadId": THREAD_ID,
                "turnId": "turn-1",
                "item": {
                    "id": "item-1",
                    "type": "commandExecution",
                    "status": "inProgress",
                    "command": "type C:\\private\\secret.txt",
                },
            },
        )

        event = self.hub.wait(0, 0)["events"][0]

        self.assertEqual(event["itemId"], "item-1")
        self.assertEqual(event["itemType"], "commandExecution")
        self.assertEqual(event["status"], "inProgress")
        self.assertNotIn("command", event)
        self.assertNotIn("secret", str(event))


if __name__ == "__main__":
    unittest.main()
