from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .channels import ChannelConfig, ProbeResult
from .codex_adapter import CodexAdapterError, DefiniteSendFailure
from .decision import Decision, DecisionInput, decide
from .models import SessionSnapshot
from .secrets import SecretStore
from .store import ResumeOutcomePersistenceError, WatchdogStore


class SessionAdapter(Protocol):
    def read_thread(self, thread_id: str) -> SessionSnapshot:
        raise NotImplementedError

    def start_turn(self, thread_id: str, prompt: str) -> str:
        raise NotImplementedError


Probe = Callable[[ChannelConfig], ProbeResult]

CHANNEL_CATEGORIES = frozenset(
    {
        "healthy",
        "auth_error",
        "rate_limited",
        "upstream_error",
        "other_http_error",
        "network_error",
        "protocol_error",
    }
)
PROBE_DETAILS = {
    "healthy": "channel responded normally",
    "auth_error": "channel authentication failed",
    "rate_limited": "channel was rate limited",
    "upstream_error": "channel upstream was unavailable",
    "other_http_error": "channel returned an HTTP error",
    "network_error": "channel network request failed",
    "protocol_error": "channel response was invalid",
}
SESSION_STATES = frozenset(
    {
        "notLoaded",
        "idle",
        "active",
        "systemError",
        "completed",
        "inProgress",
        "failed",
        "interrupted",
        "unavailable",
        "monitor_error",
    }
)
TURN_ID = re.compile(
    r"^(?:turn-[A-Za-z0-9][A-Za-z0-9._-]{0,119}|"
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$"
)
DECISION_DETAILS = {
    "resume_candidate": "enabled recovery rule matched",
    "resume_candidate_observed": "enabled recovery rule matched",
    "resume_sent": "resume request was accepted",
    "resume_action_failed": "resume request was rejected",
    "silent_already_handled": "incident was already handled",
    "sending_in_progress": "incident send is already in progress",
    "retry_waiting": "incident retry is waiting",
    "silent_codex_unavailable": "session data was unavailable",
    "silent_channel_unavailable": "channel was unavailable",
    "silent_manual_attention": "session requires manual attention",
    "silent_no_turn": "session has no turn",
    "silent_session_running": "session is running",
    "silent_session_completed": "session is completed",
    "silent_interrupted_without_error": (
        "latest turn was interrupted without a recoverable API error"
    ),
    "silent_unknown": "session state was not recognized",
    "silent_unrecoverable_error": "no enabled recovery rule matched",
    "silent_monitor_error": "monitoring failed",
}


def _add_minutes(value: str, minutes: int) -> str:
    current = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (current + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds(value: str, seconds: int) -> str:
    current = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (current + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_detail(detail: object, api_key: str = "") -> str:
    value = str(detail or "")
    if api_key:
        value = value.replace(api_key, "[redacted]")
    return value[:500]


def _error_category(error_signature: str) -> str:
    if not error_signature:
        return ""
    if error_signature.startswith("message:"):
        return "message"
    if ":http:" in error_signature:
        return "http_status"
    if error_signature.endswith(":kind"):
        return "error_kind"
    return "decision_error"


def _channel_status(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in CHANNEL_CATEGORIES else "protocol_error"


def _normalize_probe_result(result: ProbeResult, now: str) -> ProbeResult:
    category = _channel_status(result.category) or "protocol_error"
    http_status = (
        result.http_status
        if isinstance(result.http_status, int)
        and not isinstance(result.http_status, bool)
        and 100 <= result.http_status <= 599
        else None
    )
    duration_ms = (
        result.duration_ms
        if isinstance(result.duration_ms, int)
        and not isinstance(result.duration_ms, bool)
        and result.duration_ms >= 0
        else 0
    )
    checked_at = result.checked_at
    try:
        datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        checked_at = now
    return ProbeResult(
        category,
        http_status,
        PROBE_DETAILS[category],
        duration_ms,
        checked_at,
    )


def _healthy_probe_is_fresh(result: ProbeResult, now: str) -> bool:
    if result.category != "healthy":
        return True
    try:
        checked = datetime.strptime(result.checked_at, "%Y-%m-%dT%H:%M:%SZ")
        current = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return False
    age = (current - checked).total_seconds()
    return 0 <= age <= 60


def _session_state(value: str | None) -> str:
    return value if value in SESSION_STATES else "unknown"


def _turn_id(value: str | None) -> str | None:
    if value is None or len(value) > 128 or TURN_ID.fullmatch(value) is None:
        return None
    return value


def _decision_detail(decision: str) -> str:
    return DECISION_DETAILS.get(decision, "monitoring decision recorded")


def _incident_fingerprint(
    session_id: str, turn_id: str, error_signature: str
) -> str:
    value = f"{session_id}\0{turn_id}\0{error_signature}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _run_data(
    *,
    session_id: str,
    channel_id: str,
    now: str,
    decision: str,
    channel_status: str | None = None,
    http_status: int | None = None,
    session_state: str | None = None,
    turn_id: str | None = None,
    error_category: str = "",
    duration_ms: int | None = None,
    resume_attempt: int | None = None,
    detail: str | None = None,
) -> dict:
    return {
        "sessionId": session_id,
        "channelId": channel_id,
        "startedAt": now,
        "finishedAt": now,
        "channelStatus": _channel_status(channel_status),
        "httpStatus": http_status,
        "sessionState": _session_state(session_state),
        "turnId": _turn_id(turn_id),
        "errorCategory": error_category,
        "decision": decision,
        "resumeAttempt": resume_attempt,
        "durationMs": duration_ms,
        "detailSanitized": _safe_detail(detail or _decision_detail(decision)),
    }


class WatchdogService:
    """Orchestrate channel probes and read-only session decisions."""

    def __init__(
        self,
        store: WatchdogStore,
        secret_store: SecretStore,
        probe: Probe,
        adapter: SessionAdapter,
        *,
        now_provider: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._probe = probe
        self._adapter = adapter
        self._store.recover_interrupted_sends((now_provider or _utc_now)())

    def _probe_channel(self, channel: dict, now: str) -> ProbeResult:
        api_key = ""
        try:
            api_key = self._secret_store.unprotect(channel["encryptedKey"])
            result = self._probe(
                ChannelConfig(
                    base_url=channel["baseUrl"],
                    probe_url_override=channel.get("probeUrlOverride") or "",
                    model=channel["model"],
                    api_key=api_key,
                    timeout_seconds=float(channel["timeoutSeconds"]),
                )
            )
        except Exception as error:
            return ProbeResult(
                "network_error",
                None,
                f"channel probe failed ({type(error).__name__})",
                0,
                now,
            )
        return ProbeResult(
            result.category,
            result.http_status,
            _safe_detail(result.detail, api_key),
            result.duration_ms,
            result.checked_at or now,
        )

    def check_session(
        self,
        session_id: str,
        now: str,
        probe_cache: dict[str, ProbeResult] | None = None,
    ) -> dict:
        cache = probe_cache if probe_cache is not None else {}
        session = self._store.get_session(session_id)
        if session is None or not session["enabled"]:
            raise ValueError("monitored session is missing or disabled")
        channel = self._store.get_channel(session["channelId"])
        if channel is None or not channel["enabled"]:
            raise ValueError("API channel is missing or disabled")

        result = cache.get(channel["id"])
        if result is None or not _healthy_probe_is_fresh(result, now):
            result = self._probe_channel(channel, now)
        result = _normalize_probe_result(result, now)
        cache[channel["id"]] = result
        self._store.record_channel_probe(channel["id"], result)

        snapshot: SessionSnapshot | None = None
        if result.healthy:
            try:
                snapshot = self._adapter.read_thread(session["threadId"])
            except Exception:
                snapshot = None

        decision = decide(
            DecisionInput(result.category, snapshot, self._store.list_recovery_rules())
        )
        settings = self._store.get_settings()
        if decision.code == "resume_candidate" and not settings["resumeActionsEnabled"]:
            decision = Decision(
                "resume_candidate_observed",
                decision.error_signature,
            )

        turn = snapshot.latest_turn if snapshot is not None else None
        resume_attempt: int | None = None
        retry_delay_seconds: int | None = None
        retry_next_check_at: str | None = None
        resume_outcome: str | None = None
        resume_audit_detail: str | None = None
        if (
            decision.code == "resume_candidate"
            and settings["resumeActionsEnabled"]
            and turn is not None
        ):
            fingerprint = _incident_fingerprint(
                session["id"], turn.id, decision.error_signature
            )
            incident = self._store.begin_incident(
                {
                    "fingerprint": fingerprint,
                    "sessionId": session["id"],
                    "turnId": turn.id,
                    "errorSignature": _error_category(decision.error_signature),
                    "firstSeenAt": now,
                },
                now,
            )
            resume_attempt = incident["attemptCount"] or None
            claim_outcome = incident["claimOutcome"]
            if claim_outcome == "claimed":
                try:
                    _new_turn_id = self._adapter.start_turn(
                        session["threadId"], session["resumePrompt"]
                    )
                except DefiniteSendFailure:
                    resume_outcome = "definite_failure"
                    resume_audit_detail = "resume request was rejected"
                    retry_delay_seconds = {1: 30, 2: 120}.get(
                        incident["attemptCount"]
                    )
                    decision = Decision(
                        "resume_action_failed", decision.error_signature
                    )
                except CodexAdapterError:
                    resume_outcome = "manual_attention"
                    resume_audit_detail = (
                        "send outcome requires manual confirmation"
                    )
                    decision = Decision(
                        "resume_action_failed", decision.error_signature
                    )
                else:
                    resume_outcome = "sent"
                    decision = Decision("resume_sent", decision.error_signature)
            else:
                outcome_decisions = {
                    "already_handled": "silent_already_handled",
                    "sending_in_progress": "sending_in_progress",
                    "manual_attention": "silent_manual_attention",
                    "retry_waiting": "retry_waiting",
                }
                decision = Decision(
                    outcome_decisions[claim_outcome], decision.error_signature
                )
                if claim_outcome == "retry_waiting":
                    retry_delay = {1: 30, 2: 120}.get(
                        incident["attemptCount"]
                    )
                    if retry_delay is not None and incident["lastAttemptAt"]:
                        try:
                            retry_next_check_at = _add_seconds(
                                incident["lastAttemptAt"], retry_delay
                            )
                        except ValueError:
                            retry_next_check_at = None
        state = turn.status if turn is not None else (
            snapshot.thread_status if snapshot is not None else "unavailable"
        )
        interval = session["intervalMinutes"] or settings["defaultIntervalMinutes"]
        next_check_at = (
            retry_next_check_at
            or (
                _add_seconds(now, retry_delay_seconds)
                if retry_delay_seconds is not None
                else _add_minutes(now, int(interval))
            )
        )
        run_data = _run_data(
            session_id=session["id"],
            channel_id=channel["id"],
            now=now,
            channel_status=result.category,
            http_status=result.http_status,
            session_state=state,
            turn_id=turn.id if turn is not None else None,
            error_category=_error_category(decision.error_signature),
            decision=decision.code,
            duration_ms=result.duration_ms,
            resume_attempt=resume_attempt,
            detail=resume_audit_detail,
        )
        if resume_outcome is not None:
            run = self._store.finalize_resume_outcome(
                fingerprint, resume_outcome, now, run_data, next_check_at
            )
        else:
            run = self._store.record_monitor_result(run_data, next_check_at)
        return run

    def run_due(self, now: str) -> list[dict]:
        if not self._store.get_settings()["schedulerEnabled"]:
            return []
        cache: dict[str, ProbeResult] = {}
        runs: list[dict] = []
        for session in self._store.list_due_sessions(now):
            try:
                runs.append(self.check_session(session["id"], now, cache))
            except ResumeOutcomePersistenceError:
                continue
            except Exception:
                decision = "silent_monitor_error"
                settings = self._store.get_settings()
                interval = (
                    session["intervalMinutes"]
                    or settings["defaultIntervalMinutes"]
                )
                try:
                    fallback_run = self._store.record_monitor_result(
                        _run_data(
                            session_id=session["id"],
                            channel_id=session["channelId"],
                            now=now,
                            session_state="monitor_error",
                            decision=decision,
                        ),
                        _add_minutes(now, int(interval)),
                    )
                except Exception:
                    continue
                runs.append(fallback_run)
        return runs
