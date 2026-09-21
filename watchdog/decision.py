from __future__ import annotations

import re
from dataclasses import dataclass

from .models import SessionSnapshot, TurnSnapshot


@dataclass(frozen=True)
class DecisionInput:
    channel_category: str
    snapshot: SessionSnapshot | None
    recovery_rules: list[dict]


@dataclass(frozen=True)
class Decision:
    code: str
    error_signature: str = ""
    detail: str = ""


def validate_rules(rules: list[dict]) -> list[str]:
    invalid: list[str] = []
    for rule in rules:
        if rule.get("enabled") and rule.get("matchType") == "regex":
            try:
                re.compile(str(rule.get("pattern", "")))
            except re.error:
                invalid.append(str(rule.get("id") or rule.get("name") or rule.get("pattern") or "regex"))
    return invalid


def _error_signature(turn: TurnSnapshot) -> str:
    kind = turn.error_kind or "unknown"
    if turn.http_status is not None:
        return f"{kind}:http:{turn.http_status}"
    if turn.error_kind:
        return f"{kind}:kind"
    if turn.recovery_text:
        return "message:" + turn.recovery_text.strip().lower()[:200]
    return ""


def _matches(turn: TurnSnapshot, rule: dict) -> bool:
    if not rule.get("enabled"):
        return False
    match_type = rule.get("matchType")
    pattern = str(rule.get("pattern", ""))
    if match_type == "http_status":
        return turn.http_status is not None and pattern == str(turn.http_status)
    if match_type == "error_kind":
        return bool(turn.error_kind) and pattern == turn.error_kind
    if match_type == "message_contains":
        needle = " ".join(pattern.split()).casefold()
        message = " ".join(turn.recovery_text.split()).casefold()
        return bool(needle) and needle in message
    if match_type == "regex":
        try:
            return re.search(pattern, turn.recovery_text, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def decide(value: DecisionInput) -> Decision:
    if value.channel_category != "healthy":
        return Decision("silent_channel_unavailable", detail=value.channel_category)
    current = value.snapshot
    if current is None:
        return Decision("silent_codex_unavailable")
    if any(flag in {"waitingOnApproval", "waitingOnUserInput"} for flag in current.active_flags):
        return Decision("silent_manual_attention")
    turn = current.latest_turn
    if turn is None:
        return Decision("silent_no_turn", detail=current.thread_status)
    # A turn the App Server is still writing reports ``interrupted`` with no
    # completion timestamp. Treating that as a terminal failure both mislabels
    # a healthy session and can queue a resume for work that never stopped.
    if turn.in_flight:
        return Decision("silent_session_running")
    if turn.status == "completed":
        return Decision("silent_session_completed")
    if turn.status not in {"failed", "interrupted", "systemError"}:
        return Decision("silent_unknown", detail=turn.status)
    # Codex can end a turn with a bare ``interrupted`` right after a turn that
    # failed with a real API error. The newest turn then carries no evidence and
    # the session was never resumed, so match rules against the newest turn that
    # still explains the interruption.
    evidence = current.evidence_turn or turn
    signature = _error_signature(evidence)
    matched = bool(signature) and any(
        _matches(evidence, rule) for rule in value.recovery_rules
    )
    if matched:
        return Decision("resume_candidate", signature, "enabled recovery rule matched")
    if not signature:
        return Decision(
            "silent_interrupted_without_error",
            detail="latest turn was interrupted without a recoverable API error",
        )
    return Decision("silent_unrecoverable_error", signature, "no enabled recovery rule matched")
