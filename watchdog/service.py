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
    value = str(detail or "")[:500]
    return value.replace(api_key, "[redacted]") if api_key else value


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

    def _probe_channel(self, channel: dict, now: str) -> ProbeResult:
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
        session = self._store.get_session(session_id)
        if session is None or not session["enabled"]:
            raise ValueError("monitored session is missing or disabled")
        channel = self._store.get_channel(session["channelId"])
        if channel is None or not channel["enabled"]:
            raise ValueError("API channel is missing or disabled")

        cache = probe_cache if probe_cache is not None else {}
        result = cache.get(channel["id"])
        if result is None:
            result = self._probe_channel(channel, now)
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
                decision.detail,
            )

        turn = snapshot.latest_turn if snapshot is not None else None
        state = turn.status if turn is not None else (
            snapshot.thread_status if snapshot is not None else "unavailable"
        )
        interval = session["intervalMinutes"] or settings["defaultIntervalMinutes"]
        run = self._store.create_monitor_run(
            {
                "sessionId": session["id"],
                "channelId": channel["id"],
                "startedAt": now,
                "finishedAt": now,
                "channelStatus": result.category,
                "httpStatus": result.http_status,
                "sessionState": state,
                "turnId": turn.id if turn is not None else None,
                "errorCategory": decision.error_signature,
                "decision": decision.code,
                "durationMs": result.duration_ms,
                "detailSanitized": decision.detail or result.detail,
            }
        )
        self._store.set_next_check(
            session["id"],
            now,
            _add_minutes(now, int(interval)),
            state,
            decision.code,
            turn.id if turn is not None else None,
        )
        return run

    def run_due(self, now: str) -> list[dict]:
        cache: dict[str, ProbeResult] = {}
        runs: list[dict] = []
        for session in self._store.list_due_sessions(now):
            try:
                runs.append(self.check_session(session["id"], now, cache))
            except Exception as error:
                decision = "silent_monitor_error"
                runs.append(self._store.create_monitor_run(
                    {
                        "sessionId": session["id"],
                        "channelId": session["channelId"],
                        "startedAt": now,
                        "finishedAt": now,
                        "sessionState": "monitor_error",
                        "decision": decision,
                        "detailSanitized": (
                            f"monitoring failed ({type(error).__name__})"
                        ),
                    }
                ))
                settings = self._store.get_settings()
                interval = (
                    session["intervalMinutes"]
                    or settings["defaultIntervalMinutes"]
                )
                self._store.set_next_check(
                    session["id"],
                    now,
                    _add_minutes(now, int(interval)),
                    "monitor_error",
                    decision,
                    None,
                )
        return runs
