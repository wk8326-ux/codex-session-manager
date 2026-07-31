from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .channels import ChannelConfig, ProbeResult
from .decision import Decision, DecisionInput, decide
from .models import SessionSnapshot
from .secrets import SecretStore
from .store import WatchdogStore


class SessionAdapter(Protocol):
    def read_thread(self, thread_id: str) -> SessionSnapshot:
        raise NotImplementedError


Probe = Callable[[ChannelConfig], ProbeResult]


def _add_minutes(value: str, minutes: int) -> str:
    current = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (current + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _run_data(
    *,
    session_id: str,
    channel_id: str,
    now: str,
    decision: str,
    detail: object,
    api_key: str = "",
    channel_status: str | None = None,
    http_status: int | None = None,
    session_state: str | None = None,
    turn_id: str | None = None,
    error_category: str = "",
    duration_ms: int | None = None,
) -> dict:
    return {
        "sessionId": session_id,
        "channelId": channel_id,
        "startedAt": now,
        "finishedAt": now,
        "channelStatus": _safe_detail(channel_status, api_key),
        "httpStatus": http_status,
        "sessionState": _safe_detail(session_state, api_key),
        "turnId": _safe_detail(turn_id, api_key) if turn_id else None,
        "errorCategory": error_category,
        "decision": decision,
        "durationMs": duration_ms,
        "detailSanitized": _safe_detail(detail, api_key),
    }


class WatchdogService:
    """Orchestrate channel probes and read-only session decisions."""

    def __init__(
        self,
        store: WatchdogStore,
        secret_store: SecretStore,
        probe: Probe,
        adapter: SessionAdapter,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._probe = probe
        self._adapter = adapter

    def _probe_channel(self, channel: dict, now: str) -> tuple[ProbeResult, str]:
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
            return (
                ProbeResult(
                    "network_error",
                    None,
                    f"channel probe failed ({type(error).__name__})",
                    0,
                    now,
                ),
                api_key,
            )
        return (
            ProbeResult(
                result.category,
                result.http_status,
                _safe_detail(result.detail, api_key),
                result.duration_ms,
                result.checked_at or now,
            ),
            api_key,
        )

    def check_session(
        self,
        session_id: str,
        now: str,
        probe_cache: dict[str, ProbeResult] | None = None,
    ) -> dict:
        cache = probe_cache if probe_cache is not None else {}
        return self._check_session(session_id, now, cache, {})

    def _check_session(
        self,
        session_id: str,
        now: str,
        probe_cache: dict[str, ProbeResult],
        api_keys: dict[str, str],
    ) -> dict:
        session = self._store.get_session(session_id)
        if session is None or not session["enabled"]:
            raise ValueError("monitored session is missing or disabled")
        channel = self._store.get_channel(session["channelId"])
        if channel is None or not channel["enabled"]:
            raise ValueError("API channel is missing or disabled")

        result = probe_cache.get(channel["id"])
        if result is None:
            result, api_key = self._probe_channel(channel, now)
            probe_cache[channel["id"]] = result
            api_keys[channel["id"]] = api_key
        api_key = api_keys.get(channel["id"], "")
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
                decision.detail,
            )

        turn = snapshot.latest_turn if snapshot is not None else None
        state = turn.status if turn is not None else (
            snapshot.thread_status if snapshot is not None else "unavailable"
        )
        interval = session["intervalMinutes"] or settings["defaultIntervalMinutes"]
        next_check_at = _add_minutes(now, int(interval))
        run = self._store.record_monitor_result(
            _run_data(
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
                detail=decision.detail or result.detail,
                api_key=api_key,
            ),
            next_check_at,
        )
        return run

    def run_due(self, now: str) -> list[dict]:
        cache: dict[str, ProbeResult] = {}
        api_keys: dict[str, str] = {}
        runs: list[dict] = []
        for session in self._store.list_due_sessions(now):
            try:
                runs.append(
                    self._check_session(session["id"], now, cache, api_keys)
                )
            except Exception as error:
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
                            detail=f"monitoring failed ({type(error).__name__})",
                            api_key=api_keys.get(session["channelId"], ""),
                        ),
                        _add_minutes(now, int(interval)),
                    )
                except Exception:
                    continue
                runs.append(fallback_run)
        return runs
