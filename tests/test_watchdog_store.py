from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from watchdog.store import (
    ChannelInUseError,
    ResumeOutcomePersistenceError,
    WatchdogStore,
    WatchdogStoreError,
)


THREAD_ID = "00000000-0000-4000-8000-000000000001"


class WatchdogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "watchdog.db"
        self.store = WatchdogStore(self.database_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_channel(self) -> dict:
        return self.store.create_channel(
            {
                "name": "main",
                "baseUrl": "https://api.example/v1",
                "model": "test-model",
                "encryptedKey": b"cipher",
            }
        )

    def create_session(self) -> dict:
        channel = self.create_channel()
        return self.store.create_session(
            {
                "name": "session",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": 15,
                "resumePrompt": "continue current task",
                "enabled": True,
            }
        )

    def incident_data(self, session_id: str) -> dict:
        return {
            "fingerprint": "incident-fingerprint",
            "sessionId": session_id,
            "turnId": "turn-failed",
            "errorSignature": "httpConnectionFailed:http:503",
            "firstSeenAt": "2026-07-31T06:05:00Z",
        }

    def test_begin_incident_atomically_creates_and_claims_once(self) -> None:
        session = self.create_session()
        data = self.incident_data(session["id"])

        first = self.store.begin_incident(data, "2026-07-31T06:05:00Z")
        second = self.store.begin_incident(data, "2026-07-31T06:05:01Z")

        self.assertEqual(first["claimOutcome"], "claimed")
        self.assertEqual(first["status"], "sending")
        self.assertEqual(first["attemptCount"], 1)
        self.assertEqual(second["claimOutcome"], "sending_in_progress")
        self.assertEqual(second["attemptCount"], 1)

    def test_definite_failure_retries_after_30_then_120_seconds(self) -> None:
        session = self.create_session()
        data = self.incident_data(session["id"])
        self.store.begin_incident(data, "2026-07-31T06:05:00Z")
        self.store.mark_incident_failed(data["fingerprint"], "send rejected")

        too_early_second = self.store.begin_incident(
            data, "2026-07-31T06:05:29Z"
        )
        second = self.store.begin_incident(data, "2026-07-31T06:05:30Z")
        self.store.mark_incident_failed(data["fingerprint"], "send rejected")
        too_early_third = self.store.begin_incident(
            data, "2026-07-31T06:07:29Z"
        )
        third = self.store.begin_incident(data, "2026-07-31T06:07:30Z")
        self.store.mark_incident_failed(data["fingerprint"], "send rejected")
        exhausted = self.store.begin_incident(data, "2026-07-31T07:00:00Z")

        self.assertEqual(too_early_second["claimOutcome"], "retry_waiting")
        self.assertEqual(too_early_second["attemptCount"], 1)
        self.assertEqual(second["claimOutcome"], "claimed")
        self.assertEqual(second["attemptCount"], 2)
        self.assertEqual(too_early_third["claimOutcome"], "retry_waiting")
        self.assertEqual(too_early_third["attemptCount"], 2)
        self.assertEqual(third["claimOutcome"], "claimed")
        self.assertEqual(third["attemptCount"], 3)
        self.assertEqual(exhausted["claimOutcome"], "manual_attention")
        self.assertEqual(exhausted["status"], "manual_attention")

    def test_recover_interrupted_sends_requires_manual_attention(self) -> None:
        session = self.create_session()
        data = self.incident_data(session["id"])
        self.store.begin_incident(data, "2026-07-31T06:05:00Z")

        recovered = self.store.recover_interrupted_sends(
            "2026-07-31T06:06:00Z"
        )
        incident = self.store.get_incident(data["fingerprint"])
        next_claim = self.store.begin_incident(data, "2026-07-31T07:00:00Z")

        self.assertEqual(recovered, 1)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "manual_attention")
        self.assertEqual(incident["resolvedAt"], "2026-07-31T06:06:00Z")
        self.assertEqual(next_claim["claimOutcome"], "manual_attention")

    def test_resume_finalization_rolls_back_incident_run_and_schedule_together(self) -> None:
        session = self.create_session()
        data = self.incident_data(session["id"])
        self.store.begin_incident(data, "2026-07-31T06:05:00Z")
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
            self.store.finalize_resume_outcome(
                data["fingerprint"],
                "sent",
                "2026-07-31T06:05:01Z",
                {
                    "sessionId": session["id"],
                    "channelId": session["channelId"],
                    "startedAt": "2026-07-31T06:05:01Z",
                    "finishedAt": "2026-07-31T06:05:01Z",
                    "decision": "resume_sent",
                    "turnId": "turn-failed",
                    "resumeAttempt": 1,
                },
                "2026-07-31T06:20:01Z",
            )

        incident = self.store.get_incident(data["fingerprint"])
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "sending")
        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertEqual(self.store.get_session(session["id"])["nextCheckAt"], None)

    def test_definite_failure_finalization_rolls_back_when_schedule_write_fails(self) -> None:
        session = self.create_session()
        data = self.incident_data(session["id"])
        self.store.begin_incident(data, "2026-07-31T06:05:00Z")
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
            self.store.finalize_resume_outcome(
                data["fingerprint"],
                "definite_failure",
                "2026-07-31T06:05:01Z",
                {
                    "sessionId": session["id"],
                    "channelId": session["channelId"],
                    "startedAt": "2026-07-31T06:05:01Z",
                    "finishedAt": "2026-07-31T06:05:01Z",
                    "decision": "resume_action_failed",
                    "turnId": "turn-failed",
                    "resumeAttempt": 1,
                },
                "2026-07-31T06:05:31Z",
            )

        incident = self.store.get_incident(data["fingerprint"])
        self.assertIsNotNone(incident)
        self.assertEqual(incident["status"], "sending")
        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertEqual(self.store.get_session(session["id"])["nextCheckAt"], None)

    def test_defaults_and_recovery_rules_are_seeded_once(self) -> None:
        settings = self.store.get_settings()

        self.assertEqual(settings["defaultIntervalMinutes"], 15)
        self.assertEqual(settings["minimumIntervalMinutes"], 5)
        self.assertFalse(settings["resumeActionsEnabled"])
        self.assertEqual(settings["resumeDispatchMode"], "direct_app_server")
        self.assertEqual(settings["recordRetentionDays"], 14)
        self.assertEqual(settings["recordLimit"], 2000)

        self.store.initialize()

        rules = self.store.list_recovery_rules()
        self.assertEqual(
            {
                rule["pattern"]
                for rule in rules
                if rule["matchType"] == "http_status"
            },
            {"429", "502", "503", "504"},
        )
        self.assertTrue(all(rule["builtIn"] for rule in rules))

    def test_initialize_tightens_legacy_default_retention(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy-retention.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE schema_version (version INTEGER NOT NULL)"""
            )
            connection.execute("INSERT INTO schema_version(version) VALUES (3)")
            connection.execute(
                """CREATE TABLE watchdog_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    default_interval_minutes INTEGER NOT NULL DEFAULT 15,
                    minimum_interval_minutes INTEGER NOT NULL DEFAULT 5,
                    record_retention_days INTEGER NOT NULL DEFAULT 90,
                    record_limit INTEGER NOT NULL DEFAULT 10000,
                    scheduler_enabled INTEGER NOT NULL DEFAULT 1,
                    resume_actions_enabled INTEGER NOT NULL DEFAULT 0,
                    resume_dispatch_mode TEXT NOT NULL DEFAULT 'direct_app_server'
                )"""
            )
            connection.execute(
                "INSERT INTO watchdog_settings(id) VALUES (1)"
            )
            connection.commit()
        finally:
            connection.close()

        WatchdogStore(legacy_path).initialize()
        settings = WatchdogStore(legacy_path).get_settings()

        self.assertEqual(settings["recordRetentionDays"], 14)
        self.assertEqual(settings["recordLimit"], 2000)

    def test_initialize_keeps_customized_retention(self) -> None:
        legacy_path = Path(self.temp.name) / "custom-retention.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE schema_version (version INTEGER NOT NULL)"""
            )
            connection.execute("INSERT INTO schema_version(version) VALUES (3)")
            connection.execute(
                """CREATE TABLE watchdog_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    default_interval_minutes INTEGER NOT NULL DEFAULT 15,
                    minimum_interval_minutes INTEGER NOT NULL DEFAULT 5,
                    record_retention_days INTEGER NOT NULL DEFAULT 90,
                    record_limit INTEGER NOT NULL DEFAULT 10000,
                    scheduler_enabled INTEGER NOT NULL DEFAULT 1,
                    resume_actions_enabled INTEGER NOT NULL DEFAULT 0,
                    resume_dispatch_mode TEXT NOT NULL DEFAULT 'direct_app_server'
                )"""
            )
            connection.execute(
                """INSERT INTO watchdog_settings(
                       id, record_retention_days, record_limit
                   ) VALUES (1, 30, 5000)"""
            )
            connection.commit()
        finally:
            connection.close()

        WatchdogStore(legacy_path).initialize()
        settings = WatchdogStore(legacy_path).get_settings()

        self.assertEqual(settings["recordRetentionDays"], 30)
        self.assertEqual(settings["recordLimit"], 5000)

    def test_initialize_does_not_reset_retention_after_upgrade(self) -> None:
        self.store.update_settings(
            {"recordRetentionDays": 90, "recordLimit": 10000}
        )

        self.store.initialize()

        settings = self.store.get_settings()
        self.assertEqual(settings["recordRetentionDays"], 90)
        self.assertEqual(settings["recordLimit"], 10000)

    def test_custom_recovery_rule_supports_crud_without_mutating_builtins(self) -> None:
        rule = self.store.create_recovery_rule(
            {
                "name": "Model capacity",
                "pattern": "Selected model is at capacity",
                "enabled": True,
            }
        )

        self.assertEqual(rule["matchType"], "message_contains")
        self.assertFalse(rule["builtIn"])
        updated = self.store.update_recovery_rule(
            rule["id"], {"name": "Capacity warning", "enabled": False}
        )
        self.assertEqual(updated["name"], "Capacity warning")
        self.assertFalse(updated["enabled"])

        self.store.delete_recovery_rule(rule["id"])
        self.assertIsNone(self.store.get_recovery_rule(rule["id"]))
        with self.assertRaises(WatchdogStoreError):
            self.store.delete_recovery_rule("http-503")

    def test_unattended_approval_is_opt_in_per_session(self) -> None:
        session = self.create_session()

        self.assertFalse(session["unattendedApprovalsEnabled"])
        self.assertFalse(self.store.unattended_approvals_enabled(THREAD_ID))

        updated = self.store.update_session(
            session["id"], {"unattendedApprovalsEnabled": True}
        )

        self.assertTrue(updated["unattendedApprovalsEnabled"])
        self.assertTrue(self.store.unattended_approvals_enabled(THREAD_ID))

    def test_initialize_migrates_existing_session_schema(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                """CREATE TABLE monitored_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    thread_id TEXT NOT NULL UNIQUE,
                    host_kind TEXT NOT NULL DEFAULT 'local',
                    channel_id TEXT NOT NULL,
                    interval_minutes INTEGER,
                    resume_prompt TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_session_state TEXT,
                    last_turn_id TEXT,
                    last_check_result TEXT,
                    last_checked_at TEXT,
                    next_check_at TEXT,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            connection.commit()
        finally:
            connection.close()

        legacy_store = WatchdogStore(legacy_path)
        legacy_store.initialize()
        connection = sqlite3.connect(legacy_path)
        try:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(monitored_sessions)"
                )
            }
        finally:
            connection.close()

        self.assertIn("unattended_approvals_enabled", columns)

    def test_due_sessions_returns_only_enabled_due_rows(self) -> None:
        channel = self.create_channel()
        self.store.create_session(
            {
                "name": "due",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": 10,
                "resumePrompt": "continue current task",
                "enabled": True,
                "nextCheckAt": "2026-07-31T06:00:00Z",
            }
        )
        self.store.create_session(
            {
                "name": "disabled",
                "threadId": "00000000-0000-4000-8000-000000000002",
                "channelId": channel["id"],
                "intervalMinutes": 10,
                "resumePrompt": "continue current task",
                "enabled": False,
                "nextCheckAt": "2026-07-31T06:00:00Z",
            }
        )

        rows = self.store.list_due_sessions("2026-07-31T06:05:00Z")

        self.assertEqual([row["name"] for row in rows], ["due"])

    def test_channel_in_use_cannot_be_deleted(self) -> None:
        channel = self.create_channel()
        self.store.create_session(
            {
                "name": "session",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": None,
                "resumePrompt": "continue current task",
                "enabled": True,
            }
        )

        with self.assertRaises(ChannelInUseError):
            self.store.delete_channel(channel["id"])

    def test_blank_encrypted_key_update_preserves_stored_ciphertext(self) -> None:
        channel = self.create_channel()

        updated = self.store.update_channel(
            channel["id"], {"name": "renamed", "encryptedKey": b""}
        )

        self.assertEqual(updated["name"], "renamed")
        self.assertEqual(updated["encryptedKey"], b"cipher")

    def test_prune_keeps_incident_while_session_is_still_on_same_turn(self) -> None:
        channel = self.create_channel()
        session = self.store.create_session(
            {
                "name": "old",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": 15,
                "resumePrompt": "continue current task",
                "enabled": True,
                "nextCheckAt": "2026-01-01T00:00:00Z",
            }
        )
        self.store.create_monitor_run(
            {
                "sessionId": session["id"],
                "channelId": channel["id"],
                "startedAt": "2026-01-01T00:00:00Z",
                "finishedAt": "2026-01-01T00:00:01Z",
                "decision": "resume_sent",
                "turnId": "turn-old",
            }
        )
        self.store.get_or_create_incident(
            {
                "fingerprint": "fingerprint-old",
                "sessionId": session["id"],
                "turnId": "turn-old",
                "errorSignature": "httpConnectionFailed:http:503",
                "firstSeenAt": "2026-01-01T00:00:00Z",
            }
        )
        self.store.begin_resume_attempt("fingerprint-old")
        self.store.mark_incident_sent(
            "fingerprint-old", "turn-new", "2026-01-01T00:00:01Z"
        )
        self.store.set_next_check(
            session["id"],
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:15:01Z",
            "failed",
            "resume_sent",
            "turn-old",
        )

        self.store.prune_records("2026-07-31T00:00:00Z")

        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertIsNotNone(self.store.get_incident("fingerprint-old"))

    def test_session_and_channel_can_be_deleted_after_history_exists(self) -> None:
        channel = self.create_channel()
        session = self.store.create_session(
            {
                "name": "session",
                "threadId": THREAD_ID,
                "channelId": channel["id"],
                "intervalMinutes": 15,
                "resumePrompt": "continue current task",
                "enabled": True,
            }
        )
        self.store.create_monitor_run(
            {
                "sessionId": session["id"],
                "channelId": channel["id"],
                "startedAt": "2026-07-31T00:00:00Z",
                "decision": "silent_session_running",
            }
        )
        self.store.get_or_create_incident(
            {
                "fingerprint": "delete-history",
                "sessionId": session["id"],
                "turnId": "turn-old",
                "errorSignature": "http_status",
                "firstSeenAt": "2026-07-31T00:00:00Z",
            }
        )

        self.store.delete_session(session["id"])
        self.store.delete_channel(channel["id"])

        self.assertIsNone(self.store.get_session(session["id"]))
        self.assertIsNone(self.store.get_channel(channel["id"]))
        self.assertIsNone(self.store.get_incident("delete-history"))
        run = self.store.list_monitor_runs({})[0]
        self.assertIsNone(run["sessionId"])
        self.assertIsNone(run["channelId"])


if __name__ == "__main__":
    unittest.main()
