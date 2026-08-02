from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watchdog.channels import ProbeResult
from watchdog.models import SessionSnapshot, TurnSnapshot
from watchdog.service import WatchdogService
from watchdog.store import WatchdogStore, WatchdogStoreError


THREAD_ID = "11111111-2222-4333-8444-555555555555"
NOW = "2026-08-02T02:00:00Z"


class MemorySecrets:
    def protect(self, value: str) -> bytes:
        return value.encode("utf-8")

    def unprotect(self, value: bytes) -> str:
        return value.decode("utf-8")


class BridgeAdapter:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, str]] = []

    def read_thread(self, thread_id: str) -> SessionSnapshot:
        return SessionSnapshot(
            thread_id,
            "bridge target",
            "idle",
            (),
            TurnSnapshot(
                "019fbe23-5b97-7803-82c2-c2ab73915654",
                "failed",
                "upstream unavailable",
                "httpConnectionFailed",
                503,
            ),
        )

    def start_turn(self, thread_id: str, prompt: str) -> str:
        self.start_calls.append((thread_id, prompt))
        raise AssertionError("desktop bridge mode must not call the private App Server")


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WatchdogStore(Path(self.temp.name) / "watchdog.db")
        self.store.initialize()
        self.secrets = MemorySecrets()
        channel = self.store.create_channel(
            {
                "name": "main",
                "baseUrl": "https://api.example/v1",
                "model": "test-model",
                "encryptedKey": self.secrets.protect("secret"),
            }
        )
        self.session = self.store.create_session(
            {
                "name": "bridge target",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": 15,
                "resumePrompt": "continue current task",
                "enabled": True,
                "nextCheckAt": NOW,
            }
        )
        self.store.update_settings(
            {
                "resumeActionsEnabled": True,
                "resumeDispatchMode": "desktop_bridge",
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _queue_job(self) -> tuple[dict, BridgeAdapter]:
        adapter = BridgeAdapter()
        service = WatchdogService(
            self.store,
            self.secrets,
            lambda _config: ProbeResult(
                "healthy", 200, "channel responded normally", 7, NOW
            ),
            adapter,
        )
        run = service.check_session(self.session["id"], NOW)
        return run, adapter

    def test_bridge_mode_queues_without_calling_private_app_server(self) -> None:
        run, adapter = self._queue_job()

        job = self.store.claim_desktop_bridge_job(
            "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
        )

        self.assertEqual(run["decision"], "resume_queued")
        self.assertEqual(run["sessionState"], "queued")
        self.assertEqual(adapter.start_calls, [])
        self.assertIsNotNone(job)
        self.assertEqual(job["threadId"], THREAD_ID)
        self.assertEqual(job["prompt"], "continue current task")
        self.assertEqual(job["status"], "claimed")
        self.assertNotIn("leaseToken", self.store.get_desktop_bridge_status())

    def test_claim_is_single_consumer_and_expired_lease_can_be_reclaimed(self) -> None:
        self._queue_job()

        first = self.store.claim_desktop_bridge_job(
            "desktop-a", "2026-08-02T02:00:01Z", lease_seconds=30
        )
        blocked = self.store.claim_desktop_bridge_job(
            "desktop-b", "2026-08-02T02:00:20Z", lease_seconds=30
        )
        reclaimed = self.store.claim_desktop_bridge_job(
            "desktop-b", "2026-08-02T02:00:31Z", lease_seconds=30
        )

        self.assertIsNotNone(first)
        self.assertIsNone(blocked)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(first["id"], reclaimed["id"])
        self.assertNotEqual(first["leaseToken"], reclaimed["leaseToken"])
        self.assertEqual(reclaimed["claimAttemptCount"], 2)

    def test_started_and_completed_callbacks_update_original_run_idempotently(self) -> None:
        queued_run, _adapter = self._queue_job()
        job = self.store.claim_desktop_bridge_job(
            "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
        )
        assert job is not None
        resumed_turn_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"

        started = self.store.mark_desktop_bridge_started(
            job["id"], job["leaseToken"], resumed_turn_id, "2026-08-02T02:00:02Z"
        )
        duplicate_started = self.store.mark_desktop_bridge_started(
            job["id"], job["leaseToken"], resumed_turn_id, "2026-08-02T02:00:03Z"
        )
        completed = self.store.finish_desktop_bridge_job(
            job["id"],
            job["leaseToken"],
            "completed",
            "2026-08-02T02:00:10Z",
            "desktop turn completed",
        )
        duplicate_completed = self.store.finish_desktop_bridge_job(
            job["id"],
            job["leaseToken"],
            "completed",
            "2026-08-02T02:00:11Z",
            "desktop turn completed",
        )

        self.assertEqual(started["id"], queued_run["id"])
        self.assertEqual(started["decision"], "resume_started")
        self.assertEqual(started["resumedTurnId"], resumed_turn_id)
        self.assertEqual(duplicate_started, started)
        self.assertEqual(completed["decision"], "resume_completed")
        self.assertEqual(completed["sessionState"], "completed")
        self.assertEqual(duplicate_completed, completed)
        self.assertEqual(len(self.store.list_monitor_runs({})), 1)

    def test_explicit_dispatch_failure_uses_existing_retry_schedule(self) -> None:
        queued_run, _adapter = self._queue_job()
        job = self.store.claim_desktop_bridge_job(
            "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
        )
        assert job is not None

        failed = self.store.finish_desktop_bridge_job(
            job["id"],
            job["leaseToken"],
            "dispatch_failed",
            "2026-08-02T02:00:05Z",
            "desktop rejected the send request",
        )

        incident = self.store.get_incident(job["incidentFingerprint"])
        session = self.store.get_session(self.session["id"])
        self.assertEqual(failed["id"], queued_run["id"])
        self.assertEqual(failed["decision"], "resume_action_failed")
        self.assertEqual(incident["status"], "failed")
        self.assertEqual(session["nextCheckAt"], "2026-08-02T02:00:30Z")

    def test_console_restart_preserves_pending_bridge_job(self) -> None:
        self._queue_job()

        WatchdogService(
            self.store,
            self.secrets,
            lambda _config: ProbeResult(
                "healthy", 200, "channel responded normally", 7, NOW
            ),
            BridgeAdapter(),
            now_provider=lambda: "2026-08-02T02:00:10Z",
        )
        claimed = self.store.claim_desktop_bridge_job(
            "desktop-after-restart",
            "2026-08-02T02:00:11Z",
            lease_seconds=60,
        )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "claimed")
        started = self.store.mark_desktop_bridge_started(
            claimed["id"],
            claimed["leaseToken"],
            "66666666-7777-4888-8999-aaaaaaaaaaaa",
            "2026-08-02T02:00:12Z",
        )
        self.assertEqual(started["decision"], "resume_started")

    def test_completed_bridge_history_does_not_block_session_deletion(self) -> None:
        self._queue_job()
        job = self.store.claim_desktop_bridge_job(
            "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
        )
        assert job is not None
        self.store.mark_desktop_bridge_started(
            job["id"],
            job["leaseToken"],
            "66666666-7777-4888-8999-aaaaaaaaaaaa",
            "2026-08-02T02:00:02Z",
        )
        self.store.finish_desktop_bridge_job(
            job["id"],
            job["leaseToken"],
            "completed",
            "2026-08-02T02:00:10Z",
        )

        self.store.delete_session(self.session["id"])

        self.assertIsNone(self.store.get_session(self.session["id"]))
        self.assertIsNone(self.store.list_monitor_runs({})[0]["sessionId"])

    def test_deleting_session_cancels_pending_bridge_job(self) -> None:
        self._queue_job()

        self.store.delete_session(self.session["id"])

        self.assertEqual(self.store.get_desktop_bridge_status()["pending"], 0)
        self.assertIsNone(
            self.store.claim_desktop_bridge_job(
                "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
            )
        )
        self.assertIsNone(self.store.list_monitor_runs({})[0]["sessionId"])

    def test_deleting_session_invalidates_claimed_bridge_lease(self) -> None:
        self._queue_job()
        job = self.store.claim_desktop_bridge_job(
            "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
        )
        assert job is not None

        self.store.delete_session(self.session["id"])

        with self.assertRaisesRegex(
            WatchdogStoreError, "desktop bridge job does not exist"
        ):
            self.store.mark_desktop_bridge_started(
                job["id"],
                job["leaseToken"],
                "66666666-7777-4888-8999-aaaaaaaaaaaa",
                "2026-08-02T02:00:02Z",
            )

    def test_persisted_turn_status_repairs_missing_runner_finish_callback(self) -> None:
        self._queue_job()
        job = self.store.claim_desktop_bridge_job(
            "desktop-test", "2026-08-02T02:00:01Z", lease_seconds=60
        )
        assert job is not None
        resumed_turn_id = "66666666-7777-4888-8999-aaaaaaaaaaaa"
        self.store.mark_desktop_bridge_started(
            job["id"], job["leaseToken"], resumed_turn_id, "2026-08-02T02:00:02Z"
        )

        repaired = self.store.finalize_resumed_turn(
            resumed_turn_id, "completed", "2026-08-02T02:05:00Z"
        )

        bridge = self.store.get_desktop_bridge_status()
        run = self.store.list_monitor_runs({})[0]
        self.assertTrue(repaired)
        self.assertEqual(bridge["started"], 0)
        self.assertEqual(bridge["terminal"], 1)
        self.assertEqual(run["decision"], "resume_completed")


if __name__ == "__main__":
    unittest.main()
