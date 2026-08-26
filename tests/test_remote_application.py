from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from remote.application import RemoteApplication, RemoteNotFound, RemoteValidationError
from remote.approvals import RemoteApprovalBroker
from remote.events import RemoteEventHub
from remote.store import RemoteStore
from watchdog.codex_adapter import CodexAdapterError, DefiniteSendFailure


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
        self.read_thread_ids: list[str] = []
        self.read_thread_error: CodexAdapterError | None = None
        self.send_error: CodexAdapterError | None = None
        self.thread_snapshot = SimpleNamespace(
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

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        self.last_turn_limit = turn_limit
        return {"threadId": thread_id, "status": "idle", "turns": []}

    def send_message(self, thread_id: str, prompt: str, image_url: str | None = None) -> dict:
        self.sent.append((thread_id, prompt, image_url))
        if self.send_error is not None:
            raise self.send_error
        return {"turnId": "turn-2", "delivery": "started"}

    def read_thread(self, thread_id: str) -> object:
        self.read_thread_ids.append(thread_id)
        if self.read_thread_error is not None:
            raise self.read_thread_error
        return self.thread_snapshot

    def list_threads(self, limit: int) -> list[object]:
        return [
            SimpleNamespace(
                thread_id=THREAD_ID,
                name="Local Woxsheet",
                thread_status="active",
                active_flags=("waiting",),
                latest_turn=None,
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
        response = self.application.send_message(
            synced["id"],
            {"message": "继续", "clientMessageId": "phone-message-1"},
        )

        self.assertEqual(response["threadId"], THREAD_ID)
        self.assertEqual(response["clientMessageId"], "phone-message-1")
        self.assertEqual(self.adapter.sent, [(THREAD_ID, "继续", None)])
        with self.assertRaises(RemoteNotFound):
            self.application.send_message("unknown", {"message": "continue"})

    def test_repeated_client_message_id_replays_result_without_sending_twice(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        payload = {"message": "只发送一次", "clientMessageId": "phone-message-retry"}

        first = self.application.send_message(synced["id"], payload)
        replay = self.application.send_message(synced["id"], payload)

        self.assertEqual(first["turnId"], "turn-2")
        self.assertEqual(replay["turnId"], "turn-2")
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(self.adapter.sent, [(THREAD_ID, "只发送一次", None)])

    def test_definite_send_rejection_releases_the_delivery_claim_for_retry(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        payload = {"message": "重新发送", "clientMessageId": "phone-message-rejected"}
        self.adapter.send_error = DefiniteSendFailure("explicitly rejected")

        with self.assertRaises(RemoteValidationError):
            self.application.send_message(synced["id"], payload)

        self.adapter.send_error = None
        response = self.application.send_message(synced["id"], payload)
        self.assertEqual(response["turnId"], "turn-2")
        self.assertEqual(len(self.adapter.sent), 2)

    def test_client_message_id_cannot_be_reused_for_different_content(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        self.application.send_message(
            synced["id"],
            {"message": "第一条", "clientMessageId": "phone-message-conflict"},
        )

        with self.assertRaises(RemoteValidationError):
            self.application.send_message(
                synced["id"],
                {"message": "第二条", "clientMessageId": "phone-message-conflict"},
            )

        self.assertEqual(self.adapter.sent, [(THREAD_ID, "第一条", None)])

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
        self.assertNotIn("projectSummary", status)

    def test_session_detail_is_limited_to_recent_turns(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )
        detail = self.application.read_session(synced["id"])

        self.assertEqual(detail["threadId"], THREAD_ID)
        self.assertEqual(self.adapter.last_turn_limit, 12)

        self.application.read_session(synced["id"], turn_limit=6)
        self.assertEqual(
            self.adapter.last_turn_limit,
            12,
            "a smaller view should reuse the cached projection",
        )

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
        self.assertTrue(listed[0]["statusKnown"])
        self.assertEqual(self.adapter.read_thread_ids, [THREAD_ID])

        self.application.delete_synced_session(synced["id"])
        self.assertEqual(self.application.list_sessions(), [])

    def test_session_list_reads_the_latest_turn_without_opening_the_session(self) -> None:
        self.adapter.thread_snapshot = SimpleNamespace(
            thread_id=THREAD_ID,
            name="Local Woxsheet",
            thread_status="idle",
            active_flags=(),
            latest_turn=SimpleNamespace(
                status="completed",
                error_message="",
                http_status=None,
            ),
        )
        self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )

        listed = self.application.list_sessions()

        self.assertEqual(listed[0]["latestTurnStatus"], "completed")
        self.assertTrue(listed[0]["statusKnown"])
        self.assertEqual(self.adapter.read_thread_ids, [THREAD_ID])

    def test_session_summary_list_returns_without_reading_codex(self) -> None:
        synced = self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )

        listed = self.application.list_session_summaries()

        self.assertEqual(
            {key: listed[0][key] for key in synced},
            synced,
        )
        self.assertFalse(listed[0]["statusKnown"])
        self.assertEqual(listed[0]["latestTurnStatus"], "")
        self.assertEqual(self.adapter.read_thread_ids, [])

    def test_session_list_marks_an_unreadable_status_as_unknown(self) -> None:
        self.adapter.read_thread_error = CodexAdapterError("temporarily unavailable")
        self.application.create_synced_session(
            {"name": "Woxsheet", "threadId": THREAD_ID}
        )

        listed = self.application.list_sessions()

        self.assertFalse(listed[0]["statusKnown"])
        self.assertEqual(listed[0]["latestTurnStatus"], "")

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

    def test_event_hub_requests_status_resync_when_cursor_falls_behind(self) -> None:
        for index in range(514):
            self.hub.publish(
                "item/started",
                {
                    "threadId": THREAD_ID,
                    "turnId": f"turn-{index}",
                    "status": "inProgress",
                },
            )

        batch = self.hub.wait(1, 0)

        self.assertTrue(batch["resyncRequired"])
        self.assertEqual(batch["events"][0]["sequence"], 3)

    def test_event_hub_does_not_report_gap_for_contiguous_cursor(self) -> None:
        self.hub.publish(
            "turn/completed",
            {"threadId": THREAD_ID, "turn": {"id": "turn-1", "status": "completed"}},
        )

        self.assertFalse(self.hub.wait(0, 0)["resyncRequired"])

    def test_event_hub_resets_a_cursor_from_a_previous_runtime(self) -> None:
        self.hub.publish(
            "item/started",
            {"threadId": THREAD_ID, "turnId": "turn-1", "status": "inProgress"},
        )

        batch = self.hub.wait(999, 0)

        self.assertTrue(batch["resyncRequired"])
        self.assertEqual(batch["cursor"], 1)
        self.assertEqual(batch["events"], [])

    def test_event_hub_identifies_its_runtime_generation(self) -> None:
        first = self.hub.wait(0, 0)
        second = RemoteEventHub(lambda: {THREAD_ID}).wait(0, 0)

        self.assertTrue(first["streamId"])
        self.assertNotEqual(first["streamId"], second["streamId"])


if __name__ == "__main__":
    unittest.main()
