from __future__ import annotations

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
        self.store = WatchdogStore(Path(self.temp.name) / "watchdog.db")
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

        run = service.check_session(session["id"], NOW)

        self.assertEqual(run["decision"], "silent_channel_unavailable")
        self.assertEqual(adapter.read_calls, [])
        self.assertEqual(adapter.start_calls, [])
        self.assertEqual(len(probe.calls), 1)
        self.assertNotIn(API_KEY, repr(run))
        self.assertNotIn(API_KEY, repr(self.store.list_monitor_runs({})))
        channel = self.store.get_channel(session["channelId"])
        self.assertNotIn(API_KEY, repr(channel))

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


if __name__ == "__main__":
    unittest.main()
