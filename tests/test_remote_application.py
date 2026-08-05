from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote.application import RemoteApplication, RemoteNotFound, RemoteValidationError
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
        self.sent: list[tuple[str, str]] = []
        self.last_turn_limit: int | None = None

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        self.last_turn_limit = turn_limit
        return {"threadId": thread_id, "status": "idle", "turns": []}

    def send_message(self, thread_id: str, prompt: str) -> dict:
        self.sent.append((thread_id, prompt))
        return {"turnId": "turn-2", "delivery": "started"}

    def list_threads(self, limit: int) -> list[object]:
        return [
            SimpleNamespace(
                thread_id=THREAD_ID,
                name="Local Woxsheet",
                thread_status="active",
                active_flags=("waiting",),
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
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_message_is_sent_to_the_exact_registered_thread(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        response = self.application.send_message(synced["id"], {"message": "继续"})

        self.assertEqual(response["threadId"], THREAD_ID)
        self.assertEqual(self.adapter.sent, [(THREAD_ID, "继续")])
        with self.assertRaises(RemoteNotFound):
            self.application.send_message("unknown", {"message": "continue"})

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
        self.assertEqual(self.application.list_sessions(), [synced])

        self.application.delete_synced_session(synced["id"])
        self.assertEqual(self.application.list_sessions(), [])

    def test_duplicate_synced_thread_is_rejected(self) -> None:
        payload = {"name": "Woxsheet", "threadId": THREAD_ID}
        self.application.create_synced_session(payload)

        with self.assertRaises(RemoteValidationError):
            self.application.create_synced_session(payload)

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
