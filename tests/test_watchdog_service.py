from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from watchdog.channels import ProbeResult, probe_channel
from watchdog.codex_adapter import (
    CodexProtocolError,
    DefiniteSendFailure,
    UncertainSendFailure,
)
from watchdog.models import SessionSnapshot, TurnSnapshot
from watchdog.secrets import SecretStoreError
from watchdog.service import WatchdogService
from watchdog.store import ResumeOutcomePersistenceError, WatchdogStore


THREAD_ID = "00000000-0000-4000-8000-000000000001"
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
    def __init__(self, result: ProbeResult | list[ProbeResult]) -> None:
        self.result = result
        self.calls = []

    def __call__(self, config):
        self.calls.append(config)
        if isinstance(self.result, list):
            return self.result.pop(0)
        return self.result


class FailingSecretStore:
    def unprotect(self, _value: bytes) -> str:
        raise SecretStoreError("DPAPI data belongs to another user")


class FakeAdapter:
    def __init__(self, snapshot: object = None, start_results: list[object] | None = None) -> None:
        self.snapshot = snapshot
        self.start_results = list(start_results or [])
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
        if not self.start_results:
            raise AssertionError("start_turn was not expected")
        result = self.start_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if not isinstance(result, str):
            raise AssertionError("fake start result must be a turn id or exception")
        return result


class BlockingAdapter(FakeAdapter):
    def __init__(self, snapshot: SessionSnapshot) -> None:
        super().__init__(snapshot)
        self.entered = threading.Event()
        self.release = threading.Event()

    def start_turn(self, thread_id: str, prompt: str) -> str:
        self.start_calls.append((thread_id, prompt))
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test did not release blocked start_turn")
        return "turn-new"


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
    start_results: list[object] | None = None,
) -> tuple[WatchdogService, MemorySecretStore, FakeProbe, FakeAdapter]:
    secrets = MemorySecretStore()
    probe = FakeProbe(probe_result)
    adapter = FakeAdapter(snapshot, start_results)
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

    def test_known_codex_read_failure_is_recorded_with_a_safe_reason(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store,
            healthy,
            {THREAD_ID: CodexProtocolError("thread/read returned malformed data")},
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        self.assertEqual(run["decision"], "silent_codex_unavailable")
        self.assertEqual(run["sessionState"], "unavailable")
        self.assertIn("CodexProtocolError", run["detailSanitized"])
        self.assertIn("malformed data", run["detailSanitized"])

    def test_unexpected_session_reader_failure_is_not_hidden(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store,
            healthy,
            {THREAD_ID: RuntimeError("programming error")},
        )
        session = self.create_session(secrets)

        with self.assertRaisesRegex(RuntimeError, "programming error"):
            service.check_session(session["id"], NOW)

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

    def test_interrupted_turn_without_api_error_is_recorded_explicitly(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        interrupted = SessionSnapshot(
            THREAD_ID,
            "interrupted session",
            "notLoaded",
            (),
            TurnSnapshot("turn-interrupted", "interrupted"),
        )
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, interrupted
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        self.assertEqual(run["decision"], "silent_interrupted_without_error")
        self.assertEqual(run["sessionState"], "interrupted")
        self.assertEqual(
            run["detailSanitized"],
            "latest turn was interrupted without a recoverable API error",
        )
        self.assertEqual(adapter.start_calls, [])

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

    def test_same_incident_is_sent_once_then_silent_already_handled(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-new"]
        )
        session = self.create_session(secrets)

        first = service.check_session(session["id"], NOW)
        second = service.check_session(session["id"], "2026-07-31T06:05:01Z")

        self.assertEqual(first["decision"], "resume_started")
        self.assertEqual(first["resumeAttempt"], 1)
        self.assertEqual(second["decision"], "silent_already_handled")
        self.assertEqual(adapter.start_calls, [(THREAD_ID, "continue current task")])

    def test_resume_is_only_successful_after_completed_turn_event(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-new"]
        )
        session = self.create_session(
            secrets, unattendedApprovalsEnabled=True
        )

        started = service.check_session(session["id"], NOW)

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertEqual(started["decision"], "resume_started")
        self.assertEqual(started["resumedTurnId"], "turn-new")
        self.assertEqual(incident["status"], "started")
        self.assertEqual(incident["resumedTurnId"], "turn-new")

        service.handle_app_server_event(
            "turn/completed",
            {
                "threadId": THREAD_ID,
                "turn": {"id": "turn-new", "status": "completed"},
            },
            "2026-07-31T06:06:00Z",
        )

        completed = self.store.get_incident(fingerprint)
        run = self.store.list_monitor_runs({})[0]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["resolvedAt"], "2026-07-31T06:06:00Z")
        self.assertEqual(run["decision"], "resume_completed")
        self.assertEqual(run["sessionState"], "completed")

    def test_resumed_system_error_is_finalized_as_failure(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-new"]
        )
        session = self.create_session(secrets, unattendedApprovalsEnabled=True)
        service.check_session(session["id"], NOW)

        service.handle_app_server_event(
            "turn/completed",
            {"turn": {"id": "turn-new", "status": "systemError"}},
            "2026-07-31T06:06:00Z",
        )

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        run = self.store.list_monitor_runs({})[0]
        self.assertEqual(incident["status"], "resumed_failed")
        self.assertEqual(run["decision"], "resume_failed")
        self.assertEqual(run["sessionState"], "systemError")

    def test_declined_resume_approval_is_recorded_as_manual_attention(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-new"]
        )
        session = self.create_session(secrets)
        service.check_session(session["id"], NOW)

        service.handle_app_server_event(
            "item/fileChange/requestApproval",
            {
                "threadId": THREAD_ID,
                "turnId": "turn-new",
                "watchdogDecision": "decline",
            },
            "2026-07-31T06:06:00Z",
        )

        run = self.store.list_monitor_runs({})[0]
        self.assertEqual(run["decision"], "resume_manual_attention")
        self.assertIn("file change approval", run["detailSanitized"])

    def test_turn_completion_arriving_before_start_persistence_is_not_lost(self) -> None:
        class EventBeforeReturnAdapter(FakeAdapter):
            on_start = None

            def start_turn(self, thread_id: str, prompt: str) -> str:
                self.start_calls.append((thread_id, prompt))
                assert self.on_start is not None
                self.on_start()
                return "turn-racing"

        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        secrets = MemorySecretStore()
        adapter = EventBeforeReturnAdapter(failed_snapshot())
        service = WatchdogService(self.store, secrets, FakeProbe(healthy), adapter)
        adapter.on_start = lambda: service.handle_app_server_event(
            "turn/completed",
            {"turn": {"id": "turn-racing", "status": "completed"}},
            "2026-07-31T06:05:01Z",
        )
        session = self.create_session(
            secrets, unattendedApprovalsEnabled=True
        )

        run = service.check_session(session["id"], NOW)

        stored = self.store.list_monitor_runs({})[0]
        self.assertEqual(run["resumedTurnId"], "turn-racing")
        self.assertEqual(stored["decision"], "resume_completed")
        self.assertEqual(stored["sessionState"], "completed")

    def test_newer_turn_makes_unresolved_resume_require_attention(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-resumed"]
        )
        session = self.create_session(
            secrets, unattendedApprovalsEnabled=True
        )
        service.check_session(session["id"], NOW)
        adapter.snapshot = SessionSnapshot(
            THREAD_ID,
            "newer turn",
            "idle",
            (),
            TurnSnapshot("turn-newer", "completed"),
        )

        service.check_session(session["id"], "2026-07-31T06:20:00Z")

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        runs = self.store.list_monitor_runs({})
        resumed_run = next(run for run in runs if run.get("resumedTurnId"))
        self.assertEqual(incident["status"], "manual_attention")
        self.assertEqual(resumed_run["decision"], "resume_manual_attention")

    def test_live_resumed_turn_is_not_finalized_as_interrupted(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-new"]
        )
        session = self.create_session(secrets)
        service.check_session(session["id"], NOW)
        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        self.assertEqual(
            self.store.get_incident(fingerprint)["status"], "started"
        )

        # The App Server reports a turn it is still writing as ``interrupted``
        # with no completion timestamp. Finalizing it here would resolve the
        # incident and hide a resume that is still running.
        adapter.snapshot = SessionSnapshot(
            THREAD_ID,
            "resumed session",
            "active",
            (),
            TurnSnapshot(
                "turn-new",
                "interrupted",
                "",
                "",
                None,
                "",
                started_at=1788525391,
                completed_at=None,
            ),
        )

        run = service.check_session(session["id"], "2026-07-31T06:05:30Z")

        self.assertEqual(run["decision"], "silent_session_running")
        self.assertEqual(self.store.get_incident(fingerprint)["status"], "started")

    def test_resume_matches_an_error_turn_behind_an_evidence_free_interrupt(
        self,
    ) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        failed = TurnSnapshot(
            "turn-503",
            "failed",
            "upstream unavailable",
            "httpConnectionFailed",
            503,
            "",
            1788525300,
            1788525380,
        )
        # The newest turn carries no evidence, but the 503 right before it is
        # what actually stopped the session.
        interrupted = SessionSnapshot(
            THREAD_ID,
            "interrupted session",
            "idle",
            (),
            TurnSnapshot(
                "turn-interrupted",
                "interrupted",
                "",
                "",
                None,
                "",
                1788525391,
                1788525391,
            ),
            failed,
        )
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, interrupted, ["turn-new"]
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        self.assertEqual(run["decision"], "resume_started")
        # The incident fingerprint must point at the turn that failed, or every
        # check would open a fresh incident and defeat the retry bookkeeping.
        self.assertEqual(run["turnId"], "turn-503")
        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-503\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        self.assertIsNotNone(self.store.get_incident(fingerprint))

    def test_healthy_channel_resumes_one_503_incident_once(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        secrets = MemorySecretStore()
        channel = self.store.create_channel(
            {
                "name": "healthy-compatible-channel",
                "baseUrl": "https://api.example/v1",
                "model": "test-model",
                "encryptedKey": secrets.protect(API_KEY),
            }
        )
        session = self.store.create_session(
            {
                "name": "recoverable-session",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": None,
                "resumePrompt": "continue current task",
                "enabled": True,
                "nextCheckAt": "2026-07-31T06:00:00Z",
            }
        )
        adapter = FakeAdapter(failed_snapshot(), ["turn-new"])
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service = WatchdogService(
            self.store, secrets, FakeProbe(healthy), adapter
        )

        first = service.check_session(session["id"], NOW)
        second = service.check_session(session["id"], "2026-07-31T06:05:01Z")

        self.assertEqual(first["decision"], "resume_started", first)
        self.assertEqual(first["resumeAttempt"], 1)
        self.assertEqual(second["decision"], "silent_already_handled", second)
        self.assertEqual(
            adapter.start_calls,
            [(THREAD_ID, "continue current task")],
        )

    def test_start_turn_id_is_persisted_without_leaking_into_detail(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        resumed_turn_id = "11111111-1111-4111-8111-111111111111"
        service, secrets, _probe, _adapter = make_service(
            self.store, healthy, failed_snapshot(), [resumed_turn_id]
        )
        session = self.create_session(secrets)

        run = service.check_session(session["id"], NOW)

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertEqual(run["decision"], "resume_started")
        self.assertEqual(run["resumedTurnId"], resumed_turn_id)
        self.assertNotIn(API_KEY, repr(run))
        self.assertNotIn(API_KEY, repr(incident))

    def test_success_outcome_rolls_back_with_failed_run_persistence(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store, healthy, failed_snapshot(), ["turn-new"]
        )
        session = self.create_session(secrets)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER fail_resume_run
                   BEFORE INSERT ON monitor_runs
                   BEGIN
                       SELECT RAISE(ABORT, 'forced resume run failure');
                   END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ResumeOutcomePersistenceError):
            service.check_session(session["id"], NOW)

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "sending")
        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertEqual(
            self.store.get_session(session["id"])["nextCheckAt"],
            "2026-07-31T06:00:00Z",
        )
        self.assertEqual(len(adapter.start_calls), 1)

    def test_definite_send_failures_retry_at_most_three_times(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [
                DefiniteSendFailure("rejected one"),
                DefiniteSendFailure("rejected two"),
                DefiniteSendFailure("rejected three"),
            ],
        )
        session = self.create_session(secrets)

        runs = [
            service.check_session(session["id"], "2026-07-31T06:05:00Z"),
            service.check_session(session["id"], "2026-07-31T06:05:29Z"),
            service.check_session(session["id"], "2026-07-31T06:05:30Z"),
            service.check_session(session["id"], "2026-07-31T06:07:29Z"),
            service.check_session(session["id"], "2026-07-31T06:07:30Z"),
            service.check_session(session["id"], "2026-07-31T07:00:00Z"),
        ]

        self.assertEqual(
            [run["decision"] for run in runs],
            [
                "resume_action_failed",
                "retry_waiting",
                "resume_action_failed",
                "retry_waiting",
                "resume_action_failed",
                "silent_manual_attention",
            ],
        )
        self.assertEqual([run["resumeAttempt"] for run in runs], [1, 1, 2, 2, 3, 3])
        self.assertEqual(len(adapter.start_calls), 3)
        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "manual_attention")

    def test_definite_failure_outcome_rolls_back_with_failed_schedule_write(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [DefiniteSendFailure("rejected")],
        )
        session = self.create_session(secrets)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER fail_resume_schedule
                   BEFORE UPDATE OF next_check_at ON monitored_sessions
                   BEGIN
                       SELECT RAISE(ABORT, 'forced resume schedule failure');
                   END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ResumeOutcomePersistenceError):
            service.check_session(session["id"], NOW)

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "sending")
        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertEqual(
            self.store.get_session(session["id"])["nextCheckAt"],
            "2026-07-31T06:00:00Z",
        )
        self.assertEqual(len(adapter.start_calls), 1)

    def test_run_due_does_not_fallback_after_resume_finalization_failure(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [DefiniteSendFailure("rejected")],
        )
        session = self.create_session(secrets)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER fail_incident_finalization
                   BEFORE UPDATE OF status ON recovery_incidents
                   WHEN NEW.status = 'failed'
                   BEGIN
                       SELECT RAISE(ABORT, 'forced incident finalization failure');
                   END"""
            )
            connection.commit()
        finally:
            connection.close()

        runs = service.run_due(NOW)

        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertEqual(runs, [])
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "sending")
        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertEqual(
            self.store.get_session(session["id"])["nextCheckAt"],
            "2026-07-31T06:00:00Z",
        )
        self.assertEqual(len(adapter.start_calls), 1)

    def test_run_due_schedules_definite_retries_at_30_then_120_seconds(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [
                DefiniteSendFailure("first"),
                DefiniteSendFailure("second"),
                DefiniteSendFailure("third"),
            ],
        )
        session = self.create_session(secrets)

        first = service.run_due("2026-07-31T06:05:00Z")
        before_second = service.run_due("2026-07-31T06:05:29Z")
        second = service.run_due("2026-07-31T06:05:30Z")
        before_third = service.run_due("2026-07-31T06:07:29Z")
        third = service.run_due("2026-07-31T06:07:30Z")

        self.assertEqual([run["decision"] for run in first], ["resume_action_failed"])
        self.assertEqual(before_second, [])
        self.assertEqual([run["decision"] for run in second], ["resume_action_failed"])
        self.assertEqual(before_third, [])
        self.assertEqual([run["decision"] for run in third], ["resume_action_failed"])
        self.assertEqual(len(adapter.start_calls), 3)
        updated = self.store.get_session(session["id"])
        self.assertIsNotNone(updated)
        self.assertEqual(updated["nextCheckAt"], "2026-07-31T06:22:30Z")

    def test_early_manual_check_preserves_scheduled_retry_time(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, _adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [DefiniteSendFailure("first")],
        )
        session = self.create_session(secrets)

        first = service.check_session(session["id"], "2026-07-31T06:05:00Z")
        waiting = service.check_session(session["id"], "2026-07-31T06:05:29Z")

        self.assertEqual(first["decision"], "resume_action_failed")
        self.assertEqual(waiting["decision"], "retry_waiting")
        updated = self.store.get_session(session["id"])
        self.assertIsNotNone(updated)
        self.assertEqual(updated["nextCheckAt"], "2026-07-31T06:05:30Z")

    def test_uncertain_send_failure_is_never_retried(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [UncertainSendFailure(f"unknown outcome {API_KEY}")],
        )
        session = self.create_session(secrets)

        first = service.check_session(session["id"], NOW)
        second = service.check_session(session["id"], "2026-07-31T07:00:00Z")

        self.assertEqual(first["decision"], "resume_action_failed")
        self.assertEqual(
            first["detailSanitized"],
            "send outcome requires manual confirmation",
        )
        self.assertEqual(second["decision"], "silent_manual_attention")
        self.assertEqual(len(adapter.start_calls), 1)
        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0httpConnectionFailed:http:503".encode()
        ).hexdigest()
        incident = self.store.get_incident(fingerprint)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "manual_attention")
        self.assertNotIn(API_KEY, repr(incident))
        self.assertNotIn(API_KEY, repr(self.store.list_monitor_runs({})))

    def test_protocol_error_requires_manual_attention_without_retry(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, _probe, adapter = make_service(
            self.store,
            healthy,
            failed_snapshot(),
            [CodexProtocolError("malformed turn/start response")],
        )
        session = self.create_session(secrets)

        first = service.check_session(session["id"], NOW)
        second = service.check_session(session["id"], "2026-07-31T07:00:00Z")

        self.assertEqual(first["decision"], "resume_action_failed")
        self.assertEqual(second["decision"], "silent_manual_attention")
        self.assertEqual(len(adapter.start_calls), 1)

    def test_service_startup_recovers_stale_sending_incident(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        secrets = MemorySecretStore()
        session = self.create_session(secrets)
        signature = "httpConnectionFailed:http:503"
        fingerprint = hashlib.sha256(
            f"{session['id']}\0turn-failed\0{signature}".encode()
        ).hexdigest()
        self.store.begin_incident(
            {
                "fingerprint": fingerprint,
                "sessionId": session["id"],
                "turnId": "turn-failed",
                "errorSignature": "http_status",
                "firstSeenAt": NOW,
            },
            NOW,
        )
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        adapter = FakeAdapter(failed_snapshot(), ["turn-must-not-start"])

        service = WatchdogService(
            self.store,
            secrets,
            FakeProbe(healthy),
            adapter,
            now_provider=lambda: "2026-07-31T06:06:00Z",
        )
        run = service.check_session(session["id"], "2026-07-31T07:00:00Z")

        self.assertEqual(run["decision"], "silent_manual_attention")
        self.assertEqual(adapter.start_calls, [])
        incident = self.store.get_incident(fingerprint)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "manual_attention")
        self.assertEqual(incident["resolvedAt"], "2026-07-31T06:06:00Z")

    def test_retry_reprobes_when_cached_health_is_older_than_60_seconds(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult(
            "healthy", 200, "channel responded normally", 8, "2026-07-31T06:05:00Z"
        )
        unavailable = ProbeResult(
            "upstream_error", 503, "upstream unavailable", 9, "2026-07-31T06:07:30Z"
        )
        secrets = MemorySecretStore()
        probe = FakeProbe([healthy, unavailable])
        adapter = FakeAdapter(
            failed_snapshot(),
            [DefiniteSendFailure("first"), DefiniteSendFailure("second")],
        )
        service = WatchdogService(self.store, secrets, probe, adapter)
        session = self.create_session(secrets)
        cache: dict[str, ProbeResult] = {}

        first = service.check_session(session["id"], "2026-07-31T06:05:00Z", cache)
        second = service.check_session(session["id"], "2026-07-31T06:05:30Z", cache)
        third = service.check_session(session["id"], "2026-07-31T06:07:30Z", cache)

        self.assertEqual(first["decision"], "resume_action_failed")
        self.assertEqual(second["decision"], "resume_action_failed")
        self.assertEqual(third["decision"], "silent_channel_unavailable")
        self.assertEqual(len(probe.calls), 2)
        self.assertEqual(len(adapter.start_calls), 2)

    def test_concurrent_checks_start_the_same_incident_once(self) -> None:
        self.store.update_settings({"resumeActionsEnabled": True})
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        secrets = MemorySecretStore()
        adapter = BlockingAdapter(failed_snapshot())
        service = WatchdogService(self.store, secrets, FakeProbe(healthy), adapter)
        session = self.create_session(secrets)
        runs: list[dict] = []
        errors: list[BaseException] = []

        def check() -> None:
            try:
                runs.append(service.check_session(session["id"], NOW))
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=check)
        second = threading.Thread(target=check)
        first.start()
        self.assertTrue(adapter.entered.wait(timeout=1))
        second.start()
        second.join(timeout=2)
        adapter.release.set()
        first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(
            [run["decision"] for run in runs],
            ["resume_started", "sending_in_progress"],
        )
        self.assertEqual(adapter.start_calls, [(THREAD_ID, "continue current task")])

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
        channel = self.store.get_channel(session["channelId"])
        self.assertIsNotNone(channel)
        self.assertEqual(
            channel["lastProbeDetail"], "channel upstream was unavailable"
        )
        self.assertNotIn(cached_secret, repr(channel))

    def test_due_sessions_share_probe_and_fail_independently(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        other_thread = "00000000-0000-4000-8000-000000000002"
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

    def test_secret_recovery_failure_is_not_reported_as_a_network_error(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        secrets = MemorySecretStore()
        session = self.create_session(secrets)
        adapter = FakeAdapter()
        service = WatchdogService(
            self.store,
            FailingSecretStore(),
            FakeProbe(healthy),
            adapter,
        )

        run = service.check_session(session["id"], NOW)

        self.assertEqual(run["channelStatus"], "configuration_error")
        self.assertEqual(run["decision"], "silent_channel_unavailable")
        self.assertEqual(adapter.read_calls, [])
        channel = self.store.get_channel(session["channelId"])
        self.assertEqual(channel["lastProbeStatus"], "configuration_error")
        self.assertNotEqual(channel["lastProbeStatus"], "network_error")

    def test_unexpected_probe_failure_becomes_a_monitor_error(self) -> None:
        class BrokenProbe:
            def __call__(self, _config):
                raise RuntimeError("programming failure")

        secrets = MemorySecretStore()
        session = self.create_session(secrets)
        service = WatchdogService(
            self.store,
            secrets,
            BrokenProbe(),
            FakeAdapter(),
        )

        runs = service.run_due(NOW)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["decision"], "silent_monitor_error")
        self.assertIsNone(runs[0]["channelStatus"])
        self.assertIsNone(self.store.get_channel(session["channelId"])["lastProbeStatus"])

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
        other_thread = "00000000-0000-4000-8000-000000000002"
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

    def test_disabled_scheduler_skips_due_work_but_manual_check_still_runs(self) -> None:
        healthy = ProbeResult("healthy", 200, "channel responded normally", 8, NOW)
        service, secrets, probe, adapter = make_service(
            self.store, healthy, failed_snapshot()
        )
        session = self.create_session(secrets)
        self.store.update_settings({"schedulerEnabled": False})

        self.assertEqual(service.run_due(NOW), [])
        self.assertEqual(probe.calls, [])
        self.assertEqual(adapter.read_calls, [])

        run = service.check_session(session["id"], NOW)
        self.assertEqual(run["decision"], "resume_candidate_observed")
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(adapter.read_calls, [THREAD_ID])


if __name__ == "__main__":
    unittest.main()
