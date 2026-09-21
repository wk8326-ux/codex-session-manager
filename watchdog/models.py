from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnSnapshot:
    id: str
    status: str
    error_message: str = ""
    error_kind: str = ""
    http_status: int | None = None
    diagnostic_text: str = ""
    started_at: int | None = None
    completed_at: int | None = None

    @property
    def recovery_text(self) -> str:
        parts = (self.error_message.strip(), self.diagnostic_text.strip())
        return "\n".join(part for part in parts if part)

    @property
    def has_error_evidence(self) -> bool:
        """Whether this turn carries something a recovery rule could match."""

        return bool(self.error_kind or self.http_status is not None or self.recovery_text)

    @property
    def in_flight(self) -> bool:
        """Whether the App Server still considers this turn unfinished.

        The App Server reports a running turn as ``interrupted`` once the
        client that owned it goes away, so the status string alone cannot tell
        "still working" from "gave up". A start timestamp without a completion
        timestamp is the reliable signal: every genuinely finished turn in
        production data carried ``completedAt``, while the two turns that were
        still being written had ``completedAt: null``.
        """

        if self.status == "inProgress":
            return True
        return self.started_at is not None and self.completed_at is None


@dataclass(frozen=True)
class SessionSnapshot:
    thread_id: str
    name: str
    thread_status: str
    active_flags: tuple[str, ...]
    latest_turn: TurnSnapshot | None
    recent_error_turn: TurnSnapshot | None = None

    @property
    def evidence_turn(self) -> TurnSnapshot | None:
        """The turn whose failure should be matched against recovery rules.

        Codex can interrupt a turn without recording any error, right after a
        turn that failed with a real API error. Looking only at the newest turn
        then reports "interrupted without a recoverable error" and the session
        never resumes, so fall back to the newest turn that still carries error
        evidence.
        """

        latest = self.latest_turn
        if latest is not None and latest.has_error_evidence:
            return latest
        return self.recent_error_turn
