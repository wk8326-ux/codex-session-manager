import unittest

from watchdog.decision import DecisionInput, decide
from watchdog.models import SessionSnapshot, TurnSnapshot


def snapshot(status: str, *, kind: str = "", http: int | None = None, message: str = "", flags=()) -> SessionSnapshot:
    return SessionSnapshot("thread", "name", "idle", tuple(flags), TurnSnapshot("turn", status, message, kind, http))


class DecisionTests(unittest.TestCase):
    def test_channel_failure_short_circuits_before_session_read(self) -> None:
        result = decide(DecisionInput("upstream_error", None, []))
        self.assertEqual(result.code, "silent_channel_unavailable")

    def test_completed_and_active_turns_are_silent(self) -> None:
        for status, expected in (("completed", "silent_session_completed"), ("inProgress", "silent_session_running")):
            with self.subTest(status=status):
                self.assertEqual(decide(DecisionInput("healthy", snapshot(status), [])).code, expected)

    def test_failed_503_is_resume_candidate(self) -> None:
        rules = [{"matchType": "http_status", "pattern": "503", "enabled": True}]
        result = decide(DecisionInput("healthy", snapshot("failed", kind="httpConnectionFailed", http=503), rules))
        self.assertEqual(result.code, "resume_candidate")
        self.assertEqual(result.error_signature, "httpConnectionFailed:http:503")

    def test_interrupted_without_error_and_manual_wait_are_never_resumed(self) -> None:
        self.assertEqual(decide(DecisionInput("healthy", snapshot("interrupted"), [])).code, "silent_unknown")
        waiting = snapshot("inProgress", flags=("waitingOnUserInput",))
        self.assertEqual(decide(DecisionInput("healthy", waiting, [])).code, "silent_manual_attention")

    def test_only_enabled_exact_rules_can_resume(self) -> None:
        cases = [
            (snapshot("failed", http=429), [{"matchType": "http_status", "pattern": "429", "enabled": False}], "silent_unrecoverable_error"),
            (snapshot("failed", kind="connectionReset"), [{"matchType": "error_kind", "pattern": "connectionReset", "enabled": True}], "resume_candidate"),
            (snapshot("failed", message="request timed out"), [{"matchType": "regex", "pattern": "timed out", "enabled": True}], "resume_candidate"),
            (snapshot("failed", message="request timed out"), [{"matchType": "regex", "pattern": "[", "enabled": True}], "silent_unrecoverable_error"),
        ]
        for item, rules, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(decide(DecisionInput("healthy", item, rules)).code, expected)

    def test_missing_snapshot_or_turn_is_silent(self) -> None:
        self.assertEqual(decide(DecisionInput("healthy", None, [])).code, "silent_codex_unavailable")
        empty = SessionSnapshot("thread", "name", "notLoaded", (), None)
        self.assertEqual(decide(DecisionInput("healthy", empty, [])).code, "silent_no_turn")
