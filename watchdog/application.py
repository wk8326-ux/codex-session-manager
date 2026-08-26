from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable

from .channels import ChannelConfig, ProbeResult
from .secrets import SecretStore, SecretStoreError
from .service import CHANNEL_CATEGORIES, PROBE_DETAILS
from .store import ChannelInUseError, WatchdogStore
from .validation import (
    ValidationError,
    validate_http_url,
    validate_interval,
    validate_recovery_rule_name,
    validate_recovery_rule_pattern,
    validate_required_text,
    validate_resume_prompt,
    validate_thread_id,
)

DEFAULT_RESUME_PROMPT = "继续当前开发任务"


class ResourceNotFoundError(LookupError):
    """Raised when a requested watchdog resource does not exist."""


class ResourceConflictError(RuntimeError):
    """Raised when a watchdog mutation conflicts with persisted state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field} must be a boolean")
    return value


def _strict_positive_int(
    value: object, field: str, *, minimum: int = 1, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" between {minimum} and {maximum}" if maximum is not None else f" at least {minimum}"
        raise ValidationError(f"{field} must be{suffix}")
    return value


def _safe_probe(result: ProbeResult, now: str, api_key: str) -> ProbeResult:
    category = result.category if result.category in CHANNEL_CATEGORIES else "protocol_error"
    status = result.http_status
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    duration = result.duration_ms
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        duration = 0
    checked_at = result.checked_at
    try:
        datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        checked_at = now
    detail = str(result.detail or "").replace(api_key, "[redacted]")[:500]
    if not detail:
        detail = PROBE_DETAILS[category]
    return ProbeResult(category, status, detail, duration, checked_at)


class WatchdogApplication:
    """Public application boundary used by the watchdog HTTP router."""

    def __init__(
        self,
        store: WatchdogStore,
        secret_store: SecretStore,
        probe: Callable[[ChannelConfig], ProbeResult],
        adapter: object,
        monitor_service: object,
        scheduler: object,
        *,
        codex_connected: bool | Callable[[], bool] = True,
        now_provider: Callable[[], str] = utc_now,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._probe = probe
        self._adapter = adapter
        self._monitor_service = monitor_service
        self._scheduler = scheduler
        self._codex_connected = codex_connected
        self._now = now_provider

    def _wake_scheduler(self) -> None:
        wake = getattr(self._scheduler, "wake", None)
        if callable(wake):
            wake()

    def get_status(self) -> dict:
        settings = self._store.get_settings()
        due = [
            session.get("nextCheckAt")
            for session in self._store.list_sessions()
            if session.get("enabled") and session.get("nextCheckAt")
        ]
        connected = (
            self._codex_connected()
            if callable(self._codex_connected)
            else self._codex_connected
        )
        return {
            "schedulerRunning": bool(getattr(self._scheduler, "is_running", False)),
            "schedulerEnabled": settings["schedulerEnabled"],
            "codexConnected": bool(connected),
            "resumeActionsEnabled": settings["resumeActionsEnabled"],
            "resumeDispatchMode": settings["resumeDispatchMode"],
            "desktopBridge": self._store.get_desktop_bridge_status(),
            "nextCheckAt": min(due) if due else None,
        }

    def get_settings(self) -> dict:
        return self._store.get_settings()

    def list_recovery_rules(self) -> list[dict]:
        return self._store.list_recovery_rules()

    def get_recovery_rule(self, rule_id: str) -> dict:
        rule = self._store.get_recovery_rule(rule_id)
        if rule is None:
            raise ResourceNotFoundError("错误类型不存在。")
        return rule

    def create_recovery_rule(self, payload: dict) -> dict:
        self._reject_unknown(payload, {"name", "pattern", "enabled"})
        return self._store.create_recovery_rule(
            {
                "name": validate_recovery_rule_name(payload.get("name")),
                "pattern": validate_recovery_rule_pattern(payload.get("pattern")),
                "enabled": self._optional_bool(payload, "enabled", True),
            }
        )

    def update_recovery_rule(self, rule_id: str, payload: dict) -> dict:
        current = self._store.get_recovery_rule(rule_id)
        if current is None:
            raise ResourceNotFoundError("错误类型不存在。")
        self._reject_unknown(payload, {"name", "pattern", "enabled"})
        changes: dict = {}
        if "name" in payload:
            changes["name"] = validate_recovery_rule_name(payload["name"])
        if "pattern" in payload:
            changes["pattern"] = validate_recovery_rule_pattern(payload["pattern"])
        if "enabled" in payload:
            changes["enabled"] = _strict_bool(payload["enabled"], "enabled")
        updated = self._store.update_recovery_rule(rule_id, changes)
        assert updated is not None
        return updated

    def delete_recovery_rule(self, rule_id: str) -> None:
        if self._store.get_recovery_rule(rule_id) is None:
            raise ResourceNotFoundError("错误类型不存在。")
        self._store.delete_recovery_rule(rule_id)

    def update_settings(self, payload: dict) -> dict:
        allowed = {
            "defaultIntervalMinutes",
            "recordRetentionDays",
            "recordLimit",
            "schedulerEnabled",
            "resumeActionsEnabled",
            "resumeDispatchMode",
        }
        self._reject_unknown(payload, allowed)
        changes: dict = {}
        minimum = self._store.get_settings()["minimumIntervalMinutes"]
        if "defaultIntervalMinutes" in payload:
            value = payload["defaultIntervalMinutes"]
            if isinstance(value, bool):
                raise ValidationError("defaultIntervalMinutes must be an integer")
            changes["defaultIntervalMinutes"] = validate_interval(value, minimum=minimum)
        for field in ("recordRetentionDays", "recordLimit"):
            if field in payload:
                changes[field] = _strict_positive_int(payload[field], field)
        for field in ("schedulerEnabled", "resumeActionsEnabled"):
            if field in payload:
                changes[field] = _strict_bool(payload[field], field)
        if "resumeDispatchMode" in payload:
            mode = payload["resumeDispatchMode"]
            if mode not in {"direct_app_server", "desktop_bridge"}:
                raise ValidationError(
                    "resumeDispatchMode must be direct_app_server or desktop_bridge"
                )
            changes["resumeDispatchMode"] = mode
        updated = self._store.update_settings(changes)
        self._wake_scheduler()
        return updated

    @staticmethod
    def _safe_channel(channel: dict) -> dict:
        result = dict(channel)
        result.pop("encryptedKey", None)
        result["apiKeyMasked"] = "已保存"
        result["lastProbeCategory"] = result.pop("lastProbeStatus", None)
        return result

    def list_channels(self) -> list[dict]:
        return [self._safe_channel(channel) for channel in self._store.list_channels()]

    def get_channel(self, channel_id: str) -> dict:
        channel = self._store.get_channel(channel_id)
        if channel is None:
            raise ResourceNotFoundError("监控渠道不存在。")
        return self._safe_channel(channel)

    def create_channel(self, payload: dict) -> dict:
        allowed = {
            "name",
            "baseUrl",
            "probeUrlOverride",
            "model",
            "apiKey",
            "timeoutSeconds",
            "enabled",
        }
        self._reject_unknown(payload, allowed)
        api_key = validate_required_text(payload.get("apiKey"), "apiKey")
        now = self._now()
        data = {
            "name": validate_required_text(payload.get("name"), "name"),
            "baseUrl": validate_http_url(payload.get("baseUrl")),
            "probeUrlOverride": self._optional_url(payload.get("probeUrlOverride")),
            "model": validate_required_text(payload.get("model"), "model"),
            "encryptedKey": self._secret_store.protect(api_key),
            "timeoutSeconds": _strict_positive_int(
                payload.get("timeoutSeconds", 15),
                "timeoutSeconds",
                maximum=120,
            ),
            "enabled": self._optional_bool(payload, "enabled", True),
            "createdAt": now,
            "updatedAt": now,
        }
        return self._safe_channel(self._store.create_channel(data))

    def update_channel(self, channel_id: str, payload: dict) -> dict:
        current = self._store.get_channel(channel_id)
        if current is None:
            raise ResourceNotFoundError("监控渠道不存在。")
        allowed = {
            "name",
            "baseUrl",
            "probeUrlOverride",
            "model",
            "apiKey",
            "timeoutSeconds",
            "enabled",
        }
        self._reject_unknown(payload, allowed)
        changes: dict = {"updatedAt": self._now()}
        if "name" in payload:
            changes["name"] = validate_required_text(payload["name"], "name")
        if "baseUrl" in payload:
            changes["baseUrl"] = validate_http_url(payload["baseUrl"])
        if "probeUrlOverride" in payload:
            changes["probeUrlOverride"] = self._optional_url(payload["probeUrlOverride"])
        if "model" in payload:
            changes["model"] = validate_required_text(payload["model"], "model")
        if "timeoutSeconds" in payload:
            changes["timeoutSeconds"] = _strict_positive_int(
                payload["timeoutSeconds"], "timeoutSeconds", maximum=120
            )
        if "enabled" in payload:
            changes["enabled"] = _strict_bool(payload["enabled"], "enabled")
        if "apiKey" in payload and str(payload["apiKey"] or "").strip():
            key = validate_required_text(payload["apiKey"], "apiKey")
            changes["encryptedKey"] = self._secret_store.protect(key)
        updated = self._store.update_channel(channel_id, changes)
        assert updated is not None
        return self._safe_channel(updated)

    def delete_channel(self, channel_id: str) -> None:
        if self._store.get_channel(channel_id) is None:
            raise ResourceNotFoundError("监控渠道不存在。")
        try:
            self._store.delete_channel(channel_id)
        except ChannelInUseError as error:
            raise ResourceConflictError(
                "请先重新绑定或删除使用该渠道的监控会话。"
            ) from error

    def probe_channel(self, channel_id: str) -> dict:
        channel = self._store.get_channel(channel_id)
        if channel is None:
            raise ResourceNotFoundError("监控渠道不存在。")
        now = self._now()
        api_key = ""
        try:
            api_key = self._secret_store.unprotect(channel["encryptedKey"])
        except SecretStoreError:
            raw = ProbeResult(
                "configuration_error",
                None,
                PROBE_DETAILS["configuration_error"],
                0,
                now,
            )
        else:
            raw = self._probe(
                ChannelConfig(
                    base_url=channel["baseUrl"],
                    probe_url_override=channel.get("probeUrlOverride") or "",
                    model=channel["model"],
                    api_key=api_key,
                    timeout_seconds=float(channel["timeoutSeconds"]),
                )
            )
        result = _safe_probe(raw, now, api_key)
        self._store.record_channel_probe(channel_id, result)
        return {
            "category": result.category,
            "httpStatus": result.http_status,
            "detail": result.detail,
            "durationMs": result.duration_ms,
            "checkedAt": result.checked_at,
        }

    @staticmethod
    def _session_state(session: dict) -> str:
        state = session.get("lastSessionState")
        decision = session.get("lastCheckResult")
        if decision in {
            "silent_manual_attention",
            "resume_action_failed",
            "resume_manual_attention",
            "resume_failed",
            "resume_interrupted",
        }:
            return "attention"
        if state in {"active", "inProgress", "queued"}:
            return "running"
        if state in {"idle", "completed"}:
            return "idle"
        if state in {"failed", "interrupted", "monitor_error", "unavailable"}:
            return "attention"
        return "pending"

    def _safe_session(self, session: dict) -> dict:
        result = dict(session)
        settings = self._store.get_settings()
        result["effectiveIntervalMinutes"] = (
            result.get("intervalMinutes") or settings["defaultIntervalMinutes"]
        )
        result["state"] = self._session_state(result)
        return result

    def list_sessions(self) -> list[dict]:
        return [self._safe_session(session) for session in self._store.list_sessions()]

    def get_session(self, session_id: str) -> dict:
        session = self._store.get_session(session_id)
        if session is None:
            raise ResourceNotFoundError("监控会话不存在。")
        return self._safe_session(session)

    def _validated_channel_id(self, value: object) -> str:
        channel_id = validate_required_text(value, "channelId")
        if self._store.get_channel(channel_id) is None:
            raise ValidationError("请先在“添加监控渠道”中添加并启用渠道。")
        return channel_id

    def create_session(self, payload: dict) -> dict:
        allowed = {
            "name",
            "threadId",
            "channelId",
            "intervalMinutes",
            "resumePrompt",
            "unattendedApprovalsEnabled",
            "enabled",
        }
        self._reject_unknown(payload, allowed)
        now = self._now()
        enabled = self._optional_bool(payload, "enabled", True)
        interval = self._optional_interval(payload)
        prompt = (
            DEFAULT_RESUME_PROMPT
            if "resumePrompt" not in payload
            else validate_resume_prompt(payload["resumePrompt"])
        )
        data = {
            "name": validate_required_text(payload.get("name"), "name"),
            "threadId": validate_thread_id(payload.get("threadId")),
            "hostKind": "local",
            "channelId": self._validated_channel_id(payload.get("channelId")),
            "intervalMinutes": interval,
            "resumePrompt": prompt,
            "unattendedApprovalsEnabled": self._optional_bool(
                payload, "unattendedApprovalsEnabled", False
            ),
            "enabled": enabled,
            "nextCheckAt": now if enabled else None,
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            created = self._safe_session(self._store.create_session(data))
            self._wake_scheduler()
            return created
        except sqlite3.IntegrityError as error:
            raise ResourceConflictError("该 Codex 会话已在监控目录中。") from error

    def update_session(self, session_id: str, payload: dict) -> dict:
        current = self._store.get_session(session_id)
        if current is None:
            raise ResourceNotFoundError("监控会话不存在。")
        allowed = {
            "name",
            "threadId",
            "channelId",
            "intervalMinutes",
            "resumePrompt",
            "unattendedApprovalsEnabled",
            "enabled",
        }
        self._reject_unknown(payload, allowed)
        changes: dict = {"updatedAt": self._now()}
        if "name" in payload:
            changes["name"] = validate_required_text(payload["name"], "name")
        if "threadId" in payload:
            changes["threadId"] = validate_thread_id(payload["threadId"])
        if "channelId" in payload:
            changes["channelId"] = self._validated_channel_id(payload["channelId"])
        if "intervalMinutes" in payload:
            changes["intervalMinutes"] = self._optional_interval(payload)
        if "resumePrompt" in payload:
            changes["resumePrompt"] = validate_resume_prompt(payload["resumePrompt"])
        if "unattendedApprovalsEnabled" in payload:
            changes["unattendedApprovalsEnabled"] = _strict_bool(
                payload["unattendedApprovalsEnabled"],
                "unattendedApprovalsEnabled",
            )
        if "enabled" in payload:
            enabled = _strict_bool(payload["enabled"], "enabled")
            changes["enabled"] = enabled
            changes["nextCheckAt"] = self._now() if enabled else None
        try:
            updated = self._store.update_session(session_id, changes)
        except sqlite3.IntegrityError as error:
            raise ResourceConflictError("该 Codex 会话已在监控目录中。") from error
        assert updated is not None
        self._wake_scheduler()
        return self._safe_session(updated)

    def delete_session(self, session_id: str) -> None:
        if self._store.get_session(session_id) is None:
            raise ResourceNotFoundError("监控会话不存在。")
        self._store.delete_session(session_id)
        self._wake_scheduler()

    def check_session(self, session_id: str) -> dict:
        session = self._store.get_session(session_id)
        if session is None:
            raise ResourceNotFoundError("监控会话不存在。")
        if not session["enabled"]:
            raise ResourceConflictError("请先启用该监控会话。")
        channel = self._store.get_channel(session["channelId"])
        if channel is None or not channel["enabled"]:
            raise ResourceConflictError("请先启用绑定的监控渠道。")
        run = self._monitor_service.check_session(session_id, self._now())
        self._wake_scheduler()
        return self._safe_run(run)

    def list_local_sessions(self, limit: int) -> list[dict]:
        snapshots = self._adapter.list_threads(limit=limit)
        return [
            {
                "threadId": item.thread_id,
                "name": item.name,
                "threadStatus": item.thread_status,
                "activeFlags": list(item.active_flags),
            }
            for item in snapshots
        ]

    def list_runs(self, filters: dict) -> list[dict]:
        return [
            self._safe_run(run)
            for run in self._store.list_monitor_runs(filters)
        ]

    def get_desktop_bridge_status(self) -> dict:
        status = self._store.get_desktop_bridge_status()
        status["dispatchMode"] = self._store.get_settings()[
            "resumeDispatchMode"
        ]
        return status

    def claim_desktop_bridge_job(self, payload: dict) -> dict | None:
        self._reject_unknown(payload, {"runnerId", "leaseSeconds"})
        runner_id = validate_required_text(payload.get("runnerId"), "runnerId")
        lease_seconds = _strict_positive_int(
            payload.get("leaseSeconds", 90),
            "leaseSeconds",
            minimum=30,
            maximum=300,
        )
        return self._store.claim_desktop_bridge_job(
            runner_id,
            self._now(),
            lease_seconds=lease_seconds,
        )

    def mark_desktop_bridge_started(self, job_id: str, payload: dict) -> dict:
        self._reject_unknown(payload, {"leaseToken", "resumedTurnId"})
        lease_token = validate_required_text(
            payload.get("leaseToken"), "leaseToken"
        )
        resumed_turn_id = validate_thread_id(payload.get("resumedTurnId"))
        run = self._store.mark_desktop_bridge_started(
            validate_required_text(job_id, "jobId"),
            lease_token,
            resumed_turn_id,
            self._now(),
        )
        return self._safe_run(run)

    def finish_desktop_bridge_job(self, job_id: str, payload: dict) -> dict:
        self._reject_unknown(payload, {"leaseToken", "outcome", "detail"})
        lease_token = validate_required_text(
            payload.get("leaseToken"), "leaseToken"
        )
        outcome = validate_required_text(payload.get("outcome"), "outcome")
        if outcome not in {
            "completed",
            "failed",
            "interrupted",
            "manual_attention",
            "dispatch_failed",
        }:
            raise ValidationError("unsupported desktop bridge outcome")
        detail = str(payload.get("detail") or "")[:500]
        run = self._store.finish_desktop_bridge_job(
            validate_required_text(job_id, "jobId"),
            lease_token,
            outcome,
            self._now(),
            detail,
        )
        return self._safe_run(run)

    @staticmethod
    def _safe_run(run: dict) -> dict:
        result = dict(run)
        result["detail"] = result.pop("detailSanitized", "")
        return result

    def _optional_interval(self, payload: dict) -> int | None:
        value = payload.get("intervalMinutes")
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValidationError("intervalMinutes must be an integer")
        minimum = self._store.get_settings()["minimumIntervalMinutes"]
        return validate_interval(value, minimum=minimum)

    @staticmethod
    def _optional_url(value: object) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        return validate_http_url(value)

    @staticmethod
    def _optional_bool(payload: dict, field: str, default: bool) -> bool:
        if field not in payload:
            return default
        return _strict_bool(payload[field], field)

    @staticmethod
    def _reject_unknown(payload: dict, allowed: set[str]) -> None:
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError(f"unknown field: {sorted(unknown)[0]}")
