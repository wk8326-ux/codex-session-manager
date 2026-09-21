from __future__ import annotations

import hashlib
import re
import threading
from typing import Callable, Protocol

from timeutil import parse_utc, shift_utc, utc_now as _utc_now

from .channels import ChannelConfig, ProbeResult
from .codex_adapter import CodexAdapterError, DefiniteSendFailure
from .decision import Decision, DecisionInput, decide
from .models import SessionSnapshot
from .secrets import SecretStore, SecretStoreError
from .store import ResumeOutcomePersistenceError, WatchdogStore


class SessionAdapter(Protocol):
    def read_thread(self, thread_id: str) -> SessionSnapshot:
        raise NotImplementedError

    def start_turn(self, thread_id: str, prompt: str) -> str:
        raise NotImplementedError


def _read_session_status(adapter: object, thread_id: str) -> SessionSnapshot:
    """Prefer the cheap status read, falling back to a full thread read.

    ``read_status`` skips transcript loading and measured roughly 30ms against
    1.6-6.9s for ``read_thread`` on long sessions. Keeping the fallback means
    adapters that only implement the older protocol still work.
    """

    reader = getattr(adapter, "read_status", None)
    if callable(reader):
        return reader(thread_id)
    return adapter.read_thread(thread_id)


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
        "configuration_error",
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
    "configuration_error": "channel secret could not be read on this device",
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
        "queued",
    }
)
TERMINAL_TURN_STATES = frozenset(
    {"completed", "failed", "interrupted", "systemError"}
)
TURN_ID = re.compile(
    r"^(?:turn-[A-Za-z0-9][A-Za-z0-9._-]{0,119}|"
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$"
)
DECISION_DETAILS = {
    "resume_candidate": "enabled recovery rule matched",
    # Kept distinct from ``resume_candidate`` on purpose: this code means the
    # rule matched but the global resume switch blocked the send. Recording the
    # same text for both made "no rule matched" and "rule matched but blocked"
    # indistinguishable in the run log, which is what users kept reporting as
    # "命中规则却不续跑".
    "resume_candidate_observed": (
        "enabled recovery rule matched, but resume actions are turned off"
    ),
    "resume_started": "resumed turn started; waiting for final outcome",
    "resume_queued": "waiting for Codex Desktop bridge",
    "resume_cancelled": "bridge dispatch was cancelled because monitoring was disabled",
    "resume_completed": "resumed turn completed",
    "resume_failed": "resumed turn failed",
    "resume_interrupted": "resumed turn was interrupted",
    "resume_manual_attention": "resumed turn requires manual attention",
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
    return shift_utc(value, minutes=minutes)


def _add_seconds(value: str, seconds: int) -> str:
    return shift_utc(value, seconds=seconds)


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
        parse_utc(checked_at)
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
        checked = parse_utc(result.checked_at)
        current = parse_utc(now)
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
        self._event_lock = threading.Lock()
        self._pending_turn_events: dict[str, tuple[int, str, str, str]] = {}
        self._store.recover_interrupted_sends((now_provider or _utc_now)())

    def _probe_channel(self, channel: dict, now: str) -> ProbeResult:
        try:
            api_key = self._secret_store.unprotect(channel["encryptedKey"])
        except SecretStoreError:
            return ProbeResult(
                "configuration_error",
                None,
                PROBE_DETAILS["configuration_error"],
                0,
                now,
            )
        result = self._probe(
            ChannelConfig(
                base_url=channel["baseUrl"],
                probe_url_override=channel.get("probeUrlOverride") or "",
                model=channel["model"],
                api_key=api_key,
                timeout_seconds=float(channel["timeoutSeconds"]),
            )
        )
        return ProbeResult(
            result.category,
            result.http_status,
            _safe_detail(result.detail, api_key),
            result.duration_ms,
            result.checked_at or now,
        )

    def handle_app_server_event(
        self, method: str, params: dict, now: str | None = None
    ) -> None:
        event_time = now or _utc_now()
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                return
            turn_id = turn.get("id")
            status = turn.get("status")
            if isinstance(turn_id, str) and isinstance(status, str):
                handled = self._store.finalize_resumed_turn(
                    turn_id, status, event_time
                )
                if not handled and status in TERMINAL_TURN_STATES:
                    self._remember_turn_event(
                        turn_id, 1, "terminal", status, event_time
                    )
            return
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
        }:
            return
        if params.get("watchdogDecision") in {"accept", "acceptForSession"}:
            return
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            return
        approval_labels = {
            "item/commandExecution/requestApproval": "command approval",
            "item/fileChange/requestApproval": "file change approval",
            "item/permissions/requestApproval": "permission expansion approval",
            "item/tool/requestUserInput": "user input",
        }
        detail = f"resumed turn stopped for manual {approval_labels[method]}"
        handled = self._store.mark_resumed_turn_manual_attention(
            turn_id, detail, event_time
        )
        if not handled:
            self._remember_turn_event(
                turn_id, 2, "manual", detail, event_time
            )

    def _remember_turn_event(
        self,
        turn_id: str,
        priority: int,
        kind: str,
        value: str,
        event_time: str,
    ) -> None:
        with self._event_lock:
            previous = self._pending_turn_events.get(turn_id)
            if previous is None or priority > previous[0]:
                self._pending_turn_events[turn_id] = (
                    priority,
                    kind,
                    value,
                    event_time,
                )
            while len(self._pending_turn_events) > 100:
                oldest = next(iter(self._pending_turn_events))
                self._pending_turn_events.pop(oldest, None)

    def _replay_pending_turn_event(self, turn_id: str) -> None:
        with self._event_lock:
            pending = self._pending_turn_events.pop(turn_id, None)
        if pending is None:
            return
        _priority, kind, value, event_time = pending
        if kind == "manual":
            self._store.mark_resumed_turn_manual_attention(
                turn_id, value, event_time
            )
        else:
            self._store.finalize_resumed_turn(turn_id, value, event_time)

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
        snapshot_error = ""
        if result.healthy:
            try:
                snapshot = _read_session_status(self._adapter, session["threadId"])
            except CodexAdapterError as error:
                snapshot_error = (
                    f"Codex session read failed ({type(error).__name__}): {error}"
                )
        latest = snapshot.latest_turn if snapshot is not None else None
        if latest is not None:
            # A turn that is still being written reports ``interrupted`` with no
            # completion timestamp. Finalizing it here would mark a live resume
            # as finished and hide the session from the retry bookkeeping.
            if latest.status in TERMINAL_TURN_STATES and not latest.in_flight:
                self._store.finalize_resumed_turn(latest.id, latest.status, now)
            self._store.mark_stale_resumed_turns_manual_attention(
                session["id"], latest.id, now
            )

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
        # The incident belongs to the turn that actually failed, which may be
        # older than an evidence-free interrupting turn. Fingerprinting the
        # wrong turn would create a fresh incident on every check and defeat
        # the attempt/backoff bookkeeping.
        evidence_turn = snapshot.evidence_turn if snapshot is not None else None
        resume_attempt: int | None = None
        retry_delay_seconds: int | None = None
        retry_next_check_at: str | None = None
        resume_outcome: str | None = None
        resume_audit_detail: str | None = None
        # ``resume_candidate`` can only survive the downgrade above when resume
        # actions are enabled, so re-checking the setting here was dead weight.
        if decision.code == "resume_candidate" and evidence_turn is not None:
            fingerprint = _incident_fingerprint(
                session["id"], evidence_turn.id, decision.error_signature
            )
            incident = self._store.begin_incident(
                {
                    "fingerprint": fingerprint,
                    "sessionId": session["id"],
                    "turnId": evidence_turn.id,
                    "errorSignature": _error_category(decision.error_signature),
                    "firstSeenAt": now,
                },
                now,
            )
            resume_attempt = incident["attemptCount"] or None
            claim_outcome = incident["claimOutcome"]
            if claim_outcome == "claimed":
                if settings["resumeDispatchMode"] == "desktop_bridge":
                    decision = Decision("resume_queued", decision.error_signature)
                else:
                    try:
                        new_turn_id = self._adapter.start_turn(
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
                        resume_outcome = "started"
                        decision = Decision(
                            "resume_started", decision.error_signature
                        )
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
        # A resume decision is explained by the turn that actually failed, so
        # the run log points at that turn; ``resumedTurnId`` carries the new one.
        # Pointing the row at an evidence-free interrupting turn would make the
        # log disagree with the incident it was created from.
        recorded_turn = (
            evidence_turn
            if evidence_turn is not None and decision.code.startswith("resume")
            else turn
        )
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
            turn_id=recorded_turn.id if recorded_turn is not None else None,
            error_category=_error_category(decision.error_signature),
            decision=decision.code,
            duration_ms=result.duration_ms,
            resume_attempt=resume_attempt,
            detail=resume_audit_detail or snapshot_error or None,
        )
        if decision.code == "resume_queued":
            run_data["sessionState"] = "queued"
            run_data["finishedAt"] = None
            return self._store.queue_desktop_bridge_job(
                fingerprint,
                session["threadId"],
                session["resumePrompt"],
                now,
                run_data,
                next_check_at,
            )
        if resume_outcome == "started":
            run_data["resumedTurnId"] = new_turn_id
            run_data["sessionState"] = "inProgress"
            run_data["finishedAt"] = None
        if resume_outcome is not None:
            run = self._store.finalize_resume_outcome(
                fingerprint,
                resume_outcome,
                now,
                run_data,
                next_check_at,
                new_turn_id if resume_outcome == "started" else None,
            )
            if resume_outcome == "started":
                self._replay_pending_turn_event(new_turn_id)
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
