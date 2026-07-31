from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watchdog.store import ChannelInUseError, WatchdogStore


THREAD_ID = "019fa619-0c95-76c3-a151-9289b7510e09"


class WatchdogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WatchdogStore(Path(self.temp.name) / "watchdog.db")
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

    def test_defaults_and_recovery_rules_are_seeded_once(self) -> None:
        settings = self.store.get_settings()

        self.assertEqual(settings["defaultIntervalMinutes"], 15)
        self.assertEqual(settings["minimumIntervalMinutes"], 5)
        self.assertFalse(settings["resumeActionsEnabled"])
        self.assertEqual(settings["recordRetentionDays"], 90)
        self.assertEqual(settings["recordLimit"], 10000)

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
                "threadId": "019f73f7-38a9-7ed1-b5c5-5a55913a71dd",
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


if __name__ == "__main__":
    unittest.main()
