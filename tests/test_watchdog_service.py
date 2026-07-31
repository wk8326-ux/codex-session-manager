from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from watchdog.channels import ProbeResult
from watchdog.models import SessionSnapshot, TurnSnapshot
from watchdog.service import WatchdogService
from watchdog.store import WatchdogStore


THREAD_ID = "019fa619-0c95-76c3-a151-9289b7510e09"
NOW = "2026-07-31T06:05:00Z"
API_KEY = "sk-service-test-secret"


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[bytes, str] = {}
        self.unprotected: list[bytes] = []

    def protect(self, value: str) -> bytes:
        ciphertext = f"cipher-{len(self.values)}".encode()
        self.values[ciphertext] = value
        return ciphertext

    def unprotect(self, value: bytes) -> str:
        self.unprotected.append(value)
        return self.values[value]


class FakeProbe:
    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls = []

    def __call__(self, config):
        self.calls.append(config)
        return self.result


class FakeAdapter:
    def __init__(self, snapshot: object = None) -> None:
        self.snapshot = snapshot
        self.read_calls: list[str] = []
        self.start_calls: list[tuple[str, str]] = []

    def read_thread(self, thread_id: str) -> SessionSnapshot:
        self.read_calls.append(thread_id)
        if isinstance(self.snapshot, dict):
            value = self.snapshot[thread_id]
            if isinstance(value, BaseException):
                raise value
            return value
        if self.snapshot is None:
            raise AssertionError("read_thread was not expected")
        return self.snapshot

    def start_turn(self, thread_id: str, prompt: str) -> str:
        self.start_calls.append((thread_id, prompt))
        raise AssertionError("Task 6 must never start a turn")


def failed_snapshot(thread_id: str = THREAD_ID) -> SessionSnapshot:
    return SessionSnapshot(
        thread_id,
        "failed session",
        "idle",
        (),
        TurnSnapshot(
            "turn-failed",
            "failed",
            "upstream unavailable",
            "httpConnectionFailed",
            503,
        ),
    )


def make_service(
    store: WatchdogStore,
    probe_result: ProbeResult,
    snapshot: object = None,
) -> tuple[WatchdogService, MemorySecretStore, FakeProbe, FakeAdapter]:
    secrets = MemorySecretStore()
    probe = FakeProbe(probe_result)
    adapter = FakeAdapter(snapshot)
    service = WatchdogService(store, secrets, probe, adapter)
    return service, secrets, probe, adapter


class WatchdogServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "watchdog.db"
        self.store = WatchdogStore(self.database_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_session(self, secrets: MemorySecretStore, **changes) -> dict:
        channel = self.store.create_channel(
            {
                "name": "main",
                "baseUrl": "https://api.example/v1",
                "model": "test-model",
                "encryptedKey": secrets.protect(API_KEY),
            }
        )
        data = {
            "name": "session",
            "threadId": THREAD_ID,
            "channelId": channel["id"],
            "intervalMinutes": None,
            "resumePrompt": "continue current task",
            "enabled": True,
            "nextCheckAt": "2026-07-31T06:00:00Z",
        }
        data.update(changes)
        return self.store.create_session(data)

    def test_unhealthy_channel_short_circuits_before_adapter_access(self) -> None:
        unavailable = ProbeResult(
            "upstream_error", 503, f"HTTP 503 for {API_KEY}", 12, NOW
        )
        service, secrets, probe, adapter = make_service(self.store, unavailable)
        session = self.create_session(secrets)
        cache: dict[str, ProbeResult] = {}

        run = service.check_session(session["id"], NOW, cache)

        self.assertEqual(run["decision"], "silent_channel_unavailable")
        self.assertEqual(adapter.read_calls, [])
        self.assertEqual(adapter.start_calls, [])
        self.assertEqual(len(probe.calls), 1)
        self.assertNotIn(API_KEY, repr(run))
        self.assertNotIn(API_KEY, repr(self.store.list_monitor_runs({})))
        channel = self.store.get_channel(session["channelId"])
        self.assertNotIn(API_KEY, repr(channel))
        self.assertIsInstance(cache[session["channelId"]], ProbeResult)
        self.assertNotIn(API_KEY, repr(cache))
        self.assertFalse(
            any("api_key" in name for name in vars(service))
        )

    def test_resume_candidate_is_only_observed_when_actions_are_disabled(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, failed_snapshot()
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        self.assertEqual(run["decision"], "resume_candidate_observed")
        self.assertEqual(run["turnId"], "turn-failed")
        self.assertEqual(adapter.read_calls, [THREAD_ID])
        self.assertEqual(adapter.start_calls, [])
        stored_runs = self.store.list_monitor_runs({})
        self.assertEqual(len(stored_runs), 1)
        self.assertEqual(stored_runs[0]["decision"], "resume_candidate_observed")
        updated = self.store.get_session(session["id"])
        self.assertIsNotNone(updated)
        self.assertEqual(updated["nextCheckAt"], "2026-07-31T06:20:00Z")

    def test_error_signature_is_reduced_before_audit_persistence(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        snapshot = SessionSnapshot(
            THREAD_ID,
            "failed session",
            "idle",
            (),
            TurnSnapshot(
                "turn-failed",
                "failed",
                f"request failed with bearer {API_KEY}",
            ),
        )
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, snapshot
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        self.assertEqual(adapter.read_calls, [THREAD_ID])
        self.assertEqual(run["errorCategory"], "message")
        self.assertNotIn(API_KEY, repr(run))
        self.assertNotIn(API_KEY, repr(self.store.list_monitor_runs({})))

    def test_unknown_turn_status_is_sanitized_at_audit_boundary(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        snapshot = SessionSnapshot(
            THREAD_ID,
            "unrecognized session",
            "idle",
            (),
            TurnSnapshot("turn-unknown", API_KEY + "x" * 600),
        )
        service, secrets, _probe, _adapter = make_service(
            self.store, healthy, snapshot
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        self.assertNotIn(API_KEY, repr(run))
        self.assertNotIn(API_KEY, run["detailSanitized"])
        self.assertLessEqual(len(run["detailSanitized"]), 500)
        stored = self.store.list_monitor_runs({})[0]
        self.assertEqual(stored["detailSanitized"], run["detailSanitized"])

    def test_cached_probe_never_requires_secret_to_normalize_adapter_data(self) -> None:
        cached_secret = "sk-cached-probe-secret"
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        snapshot = SessionSnapshot(
            THREAD_ID,
            "cached session",
            "idle",
            (),
            TurnSnapshot("turn-cached", cached_secret),
        )
        service, secrets, probe, _adapter = make_service(
            self.store, healthy, snapshot
        )
        session = self.create_session(secrets)
        cache = {session["channelId"]: healthy}

        run = service.check_session(session["id"], NOW, cache)

        self.assertEqual(probe.calls, [])
        self.assertEqual(secrets.unprotected, [])
        self.assertEqual(run["sessionState"], "unknown")
        self.assertEqual(run["detailSanitized"], "session state was not recognized")
        self.assertNotIn(cached_secret, repr(run))
        self.assertNotIn(cached_secret, repr(self.store.list_monitor_runs({})))

    def test_cached_probe_detail_is_never_persisted(self) -> None:
        cached_secret = "sk-prefilled-detail-secret"
        unavailable = ProbeResult("upstream_error", 503, cached_secret, 8, NOW)
        service, secrets, probe, adapter = make_service(self.store, unavailable)
        session = self.create_session(secrets)
        cache = {session["channelId"]: unavailable}

        run = service.check_session(session["id"], NOW, cache)

        self.assertEqual(run["decision"], "silent_channel_unavailable")
        self.assertEqual(run["detailSanitized"], "channel was unavailable")
        self.assertEqual(probe.calls, [])
        self.assertEqual(secrets.unprotected, [])
        self.assertEqual(adapter.read_calls, [])
        self.assertNotIn(cached_secret, repr(run))
        self.assertNotIn(cached_secret, repr(self.store.list_monitor_runs({})))

    def test_due_sessions_share_probe_and_fail_independently(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        other_thread = "019f73f7-38a9-7ed1-b5c5-5a55913a71dd"
        snapshots = {
            THREAD_ID: object(),
            other_thread: failed_snapshot(other_thread),
        }
        service, secrets, probe, adapter = make_service(
            self.store, healthy, snapshots
        )
        first = self.create_session(secrets)
        second = self.store.create_session(
            {
                "name": "second",
                "threadId": other_thread,
                "channelId": first["channelId"],
                "intervalMinutes": 10,
                "resumePrompt": "continue second task",
                "enabled": True,
                "nextCheckAt": "2026-07-31T06:00:00Z",
            }
        )

        runs = service.run_due(NOW)

        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(len(runs), 2)
        by_session = {run["sessionId"]: run for run in runs}
        self.assertEqual(
            by_session[first["id"]]["decision"], "silent_monitor_error"
        )
        self.assertEqual(
            by_session[second["id"]]["decision"], "resume_candidate_observed"
        )
        self.assertEqual(len(self.store.list_monitor_runs({})), 2)
        self.assertCountEqual(adapter.read_calls, [THREAD_ID, other_thread])
        self.assertEqual(adapter.start_calls, [])
        self.assertEqual(len(secrets.unprotected), 1)
        self.assertEqual(
            self.store.get_session(first["id"])["nextCheckAt"],
            "2026-07-31T06:20:00Z",
        )
        self.assertEqual(
            self.store.get_session(second["id"])["nextCheckAt"],
            "2026-07-31T06:15:00Z",
        )
        self.assertNotIn(API_KEY, repr(runs))

    def test_run_and_schedule_update_roll_back_together(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store, healthy, failed_snapshot()
        )
        session = self.create_session(secrets)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER fail_next_check
                   BEFORE UPDATE OF next_check_at ON monitored_sessions
                   BEGIN
                       SELECT RAISE(ABORT, 'forced session update failure');
                   END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            service.check_session(session["id"], NOW)

        self.assertEqual(self.store.list_monitor_runs({}), [])
        unchanged = self.store.get_session(session["id"])
        self.assertIsNotNone(unchanged)
        self.assertEqual(unchanged["nextCheckAt"], "2026-07-31T06:00:00Z")

    def test_stale_session_fallback_failure_does_not_stop_due_batch(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        other_thread = "019f73f7-38a9-7ed1-b5c5-5a55913a71dd"
        snapshots = {other_thread: failed_snapshot(other_thread)}
        service, secrets, probe, adapter = make_service(
            self.store, healthy, snapshots
        )
        stale = self.create_session(
            secrets, nextCheckAt="2026-07-31T05:59:00Z"
        )
        active = self.store.create_session(
            {
                "name": "active",
                "threadId": other_thread,
                "channelId": stale["channelId"],
                "intervalMinutes": 10,
                "resumePrompt": "continue active task",
                "enabled": True,
                "nextCheckAt": "2026-07-31T06:00:00Z",
            }
        )
        list_due = self.store.list_due_sessions

        def list_then_delete(now: str) -> list[dict]:
            rows = list_due(now)
            self.store.delete_session(stale["id"])
            return rows

        self.store.list_due_sessions = list_then_delete  # type: ignore[method-assign]

        runs = service.run_due(NOW)

        self.assertEqual([run["sessionId"] for run in runs], [active["id"]])
        self.assertEqual(adapter.read_calls, [other_thread])
        self.assertEqual(len(probe.calls), 1)
        stored = self.store.list_monitor_runs({})
        self.assertEqual([run["sessionId"] for run in stored], [active["id"]])


if __name__ == "__main__":
    unittest.main()
