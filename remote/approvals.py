from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4


class ApprovalError(RuntimeError):
    """Base error for remote approval operations."""


class ApprovalNotFound(ApprovalError):
    pass


class InvalidApprovalDecision(ApprovalError):
    pass


class ApprovalDeliveryError(ApprovalError):
    pass


ApprovalResolver = Callable[[str], None]
EventPublisher = Callable[[str, dict], None]
AuditRecorder = Callable[[dict], None]


SUPPORTED_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: object, limit: int = 2_000) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _compact_json(value: object, limit: int = 2_000) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:limit]
    except (TypeError, ValueError):
        return ""


def _command_text(value: object) -> str:
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, list):
        return " ".join(_text(item, 500) for item in value if isinstance(item, str))[:2_000]
    return ""


def _presentation(method: str, params: dict) -> tuple[str, str, str]:
    reason = _text(params.get("reason"), 800)
    if method == "item/commandExecution/requestApproval":
        command = _command_text(params.get("command"))
        cwd = _text(params.get("cwd"), 500)
        return "命令执行授权", command or "Codex 请求执行一条命令", reason or cwd
    if method == "item/fileChange/requestApproval":
        root = _text(params.get("grantRoot"), 500)
        return "文件修改授权", reason or "Codex 请求修改工作区文件", root
    permissions = _compact_json(params.get("permissions"))
    return "权限扩展授权", reason or "Codex 请求扩展当前任务权限", permissions


def _available_decisions(method: str, params: dict) -> tuple[str, ...]:
    if method == "item/permissions/requestApproval":
        return ("accept", "decline")
    raw = params.get("availableDecisions")
    if not isinstance(raw, list):
        return ("acceptForSession", "decline")
    supported = [
        item for item in raw
        if isinstance(item, str) and item in {"accept", "acceptForSession"}
    ]
    return tuple(dict.fromkeys([*supported, "decline"]))


@dataclass
class _PendingApproval:
    public: dict
    resolver: ApprovalResolver
    timer: threading.Timer


class RemoteApprovalBroker:
    """Holds App Server approval requests without blocking its reader thread."""

    def __init__(
        self,
        allowed_thread_ids: Callable[[], set[str]],
        *,
        timeout_seconds: float = 120.0,
        publish_event: EventPublisher | None = None,
        record_audit: AuditRecorder | None = None,
    ) -> None:
        self._allowed_thread_ids = allowed_thread_ids
        self._timeout_seconds = max(0.01, float(timeout_seconds))
        self._publish_event = publish_event or (lambda _method, _params: None)
        self._record_audit = record_audit or (lambda _record: None)
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingApproval] = {}
        self._auto_approved_turns: dict[tuple[str, str], dict] = {}
        self._closed = False

    def offer(self, method: str, params: dict, resolver: ApprovalResolver) -> bool:
        kind = SUPPORTED_METHODS.get(method)
        thread_id = _text(params.get("threadId"), 200)
        if not kind or not thread_id or thread_id not in self._allowed_thread_ids():
            return False
        created = _utc_now()
        approval_id = str(uuid4())
        title, summary, detail = _presentation(method, params)
        public = {
            "id": approval_id,
            "threadId": thread_id,
            "turnId": _text(params.get("turnId"), 200),
            "itemId": _text(params.get("itemId"), 200),
            "method": method,
            "kind": kind,
            "title": title,
            "summary": summary,
            "detail": detail,
            "availableDecisions": list(_available_decisions(method, params)),
            "createdAt": _timestamp(created),
            "expiresAt": _timestamp(
                created + timedelta(seconds=self._timeout_seconds)
            ),
        }
        timer = threading.Timer(self._timeout_seconds, self._expire, (approval_id,))
        timer.daemon = True
        pending = _PendingApproval(public, resolver, timer)
        with self._lock:
            if self._closed:
                return False
            actor = self._auto_approved_turns.get((thread_id, public["turnId"]))
            if actor is None:
                self._pending[approval_id] = pending
        if actor is not None:
            decision = self._accept_decision(public)
            if decision is None:
                return False
            self._finish(
                pending,
                decision,
                actor=actor,
                outcome="auto_approved",
                raise_delivery_error=False,
            )
            return True
        timer.start()
        self._publish(
            "remote/approvalRequested",
            public,
            status="pending",
        )
        return True

    def list_pending(self) -> list[dict]:
        allowed = self._allowed_thread_ids()
        with self._lock:
            items = [
                dict(pending.public)
                for pending in self._pending.values()
                if pending.public["threadId"] in allowed
            ]
        return sorted(items, key=lambda item: (item["createdAt"], item["id"]))

    def resolve(self, approval_id: str, decision: str, actor: dict) -> dict:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                raise ApprovalNotFound("待处理授权不存在或已经结束。")
            if decision not in pending.public["availableDecisions"]:
                raise InvalidApprovalDecision("该授权请求不支持所选操作。")
            self._pending.pop(approval_id)
        pending.timer.cancel()
        return self._finish(
            pending,
            decision,
            actor=actor,
            outcome="resolved",
            raise_delivery_error=True,
        )

    def allow_turn(self, approval_id: str, actor: dict) -> dict:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                raise ApprovalNotFound("待处理授权不存在或已经结束。")
            turn_id = pending.public["turnId"]
            decision = self._accept_decision(pending.public)
            if not turn_id or decision is None:
                raise InvalidApprovalDecision("该授权请求不能应用本轮全部允许。")
            self._pending.pop(approval_id)
            self._auto_approved_turns[(pending.public["threadId"], turn_id)] = dict(actor)
            while len(self._auto_approved_turns) > 256:
                self._auto_approved_turns.pop(next(iter(self._auto_approved_turns)))
        pending.timer.cancel()
        return self._finish(
            pending,
            decision,
            actor=actor,
            outcome="resolved",
            raise_delivery_error=True,
        )

    def cancel_thread(self, thread_id: str) -> None:
        with self._lock:
            stale = [key for key in self._auto_approved_turns if key[0] == thread_id]
            for key in stale:
                self._auto_approved_turns.pop(key, None)
        self._cancel_matching(
            lambda pending: pending.public["threadId"] == thread_id,
            outcome="session_removed",
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._auto_approved_turns.clear()
        self._cancel_matching(lambda _pending: True, outcome="shutdown")

    @staticmethod
    def _accept_decision(public: dict) -> str | None:
        decisions = public.get("availableDecisions", [])
        if "acceptForSession" in decisions:
            return "acceptForSession"
        if "accept" in decisions:
            return "accept"
        return None

    def _expire(self, approval_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(approval_id, None)
        if pending is None:
            return
        self._finish(
            pending,
            "decline",
            actor={"id": "system", "name": "超时策略"},
            outcome="expired",
            raise_delivery_error=False,
        )

    def _cancel_matching(self, predicate, *, outcome: str) -> None:
        with self._lock:
            matches = [
                pending for pending in self._pending.values() if predicate(pending)
            ]
            for pending in matches:
                self._pending.pop(pending.public["id"], None)
        for pending in matches:
            pending.timer.cancel()
            self._finish(
                pending,
                "decline",
                actor={"id": "system", "name": "控制台安全策略"},
                outcome=outcome,
                raise_delivery_error=False,
            )

    def _finish(
        self,
        pending: _PendingApproval,
        decision: str,
        *,
        actor: dict,
        outcome: str,
        raise_delivery_error: bool,
    ) -> dict:
        delivery_error: BaseException | None = None
        try:
            pending.resolver(decision)
        except BaseException as error:
            delivery_error = error
        resolved_at = _timestamp(_utc_now())
        actual_outcome = "delivery_failed" if delivery_error is not None else outcome
        record = {
            **pending.public,
            "decision": decision,
            "outcome": actual_outcome,
            "actorDeviceId": _text(actor.get("id"), 200),
            "actorDeviceName": _text(actor.get("name"), 200),
            "resolvedAt": resolved_at,
        }
        try:
            self._record_audit(record)
        except Exception:
            pass
        self._publish(
            "remote/approvalResolved",
            pending.public,
            status=actual_outcome,
        )
        if delivery_error is not None and raise_delivery_error:
            raise ApprovalDeliveryError("授权结果未能送达 Codex App Server。") from delivery_error
        return {
            "id": pending.public["id"],
            "threadId": pending.public["threadId"],
            "decision": decision,
            "status": actual_outcome,
            "resolvedAt": resolved_at,
        }

    def _publish(self, method: str, public: dict, *, status: str) -> None:
        try:
            self._publish_event(
                method,
                {
                    "threadId": public["threadId"],
                    "turnId": public["turnId"],
                    "approvalId": public["id"],
                    "status": status,
                },
            )
        except Exception:
            pass
