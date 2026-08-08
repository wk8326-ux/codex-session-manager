from __future__ import annotations

import base64
import binascii
import re
import sqlite3
from urllib.parse import urlparse

from watchdog.codex_adapter import CodexAdapterError
from watchdog.validation import (
    ValidationError,
    validate_required_text,
    validate_thread_id,
)

from .approvals import (
    ApprovalDeliveryError,
    ApprovalNotFound,
    InvalidApprovalDecision,
    RemoteApprovalBroker,
)
from .events import RemoteEventHub
from .store import PairingRejected, RemoteStore


class RemoteApplicationError(RuntimeError):
    pass


class RemoteNotFound(RemoteApplicationError):
    pass


class RemoteValidationError(RemoteApplicationError):
    pass


_IMAGE_DATA_URL = re.compile(
    r"\Adata:image/(?P<type>png|jpeg|webp);base64,(?P<data>[A-Za-z0-9+/]*={0,2})\Z",
    re.ASCII,
)
_MAX_IMAGE_BYTES = 1_200_000


def _validated_image_url(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RemoteValidationError("\u622a\u56fe\u683c\u5f0f\u65e0\u6548\u3002")
    match = _IMAGE_DATA_URL.fullmatch(value)
    if match is None:
        raise RemoteValidationError(
            "\u4ec5\u652f\u6301 PNG\u3001JPEG \u6216 WebP \u622a\u56fe\u3002"
        )
    try:
        decoded = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as error:
        raise RemoteValidationError("\u622a\u56fe\u6570\u636e\u65e0\u6548\u3002") from error
    if not decoded or len(decoded) > _MAX_IMAGE_BYTES:
        raise RemoteValidationError("\u622a\u56fe\u8d85\u8fc7 1.2 MB \u9650\u5236\u3002")
    image_type = match.group("type")
    signature_is_valid = (
        (image_type == "png" and decoded.startswith(b"\x89PNG\r\n\x1a\n"))
        or (image_type == "jpeg" and decoded.startswith(b"\xff\xd8\xff"))
        or (
            image_type == "webp"
            and len(decoded) >= 12
            and decoded.startswith(b"RIFF")
            and decoded[8:12] == b"WEBP"
        )
    )
    if not signature_is_valid:
        raise RemoteValidationError(
            "\u622a\u56fe\u5185\u5bb9\u4e0e\u6587\u4ef6\u7c7b\u578b\u4e0d\u5339\u914d\u3002"
        )
    return value


class RemoteApplication:
    def __init__(
        self,
        remote_store: RemoteStore,
        adapter: object,
        event_hub: RemoteEventHub,
        project_provider,
        *,
        default_base_url: str,
        codex_connected: bool,
        approval_broker: RemoteApprovalBroker | None = None,
        tunnel_status_provider=None,
        tunnel_start_provider=None,
    ) -> None:
        self.remote_store = remote_store
        self.adapter = adapter
        self.event_hub = event_hub
        self.project_provider = project_provider
        self.default_base_url = default_base_url
        self.codex_connected = codex_connected
        self.approval_broker = approval_broker
        self.tunnel_status_provider = tunnel_status_provider
        self.tunnel_start_provider = tunnel_start_provider

    @staticmethod
    def _base_url(value: object) -> str:
        raw = str(value or "").strip().rstrip("/")
        parsed = urlparse(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RemoteValidationError("请填写有效的 HTTP 或 HTTPS 远程访问根地址。")
        return raw

    def admin_status(self) -> dict:
        base_url = self.remote_store.get_public_base_url() or self.default_base_url
        projects = self.project_provider()
        local_projects = [
            project for project in projects if project.get("mode") != "external"
        ]
        tunnel = (
            self.tunnel_status_provider()
            if self.tunnel_status_provider is not None
            else {
                "provider": "frp",
                "configured": False,
                "running": False,
                "state": "not-configured",
                "pid": None,
                "startedAt": "",
                "detail": "",
            }
        )
        return {
            "enabled": True,
            "codexConnected": self.codex_connected,
            "baseUrl": base_url,
            "deviceCount": len(self.remote_store.list_devices()),
            "projectSummary": {
                "runningCount": sum(
                    project.get("state") == "running" for project in local_projects
                ),
                "localCount": len(local_projects),
            },
            "tunnel": tunnel,
        }

    def create_pairing(self, payload: dict) -> dict:
        base_url = self._base_url(
            payload.get("baseUrl")
            or self.remote_store.get_public_base_url()
            or self.default_base_url
        )
        self.remote_store.set_public_base_url(base_url)
        pairing = self.remote_store.create_pairing(lifetime_seconds=180)
        pairing["pairingUrl"] = (
            f"{base_url}/pair?pairing={pairing['id']}#secret={pairing['secret']}"
        )
        return pairing

    def list_devices(self) -> list[dict]:
        return self.remote_store.list_devices()

    def revoke_device(self, device_id: str) -> None:
        if not self.remote_store.revoke_device(device_id):
            raise RemoteNotFound("设备不存在或已撤销。")

    def claim_pairing(self, pairing_id: str, payload: dict) -> dict:
        secret = str(payload.get("secret") or "")
        device_name = str(payload.get("deviceName") or "移动设备")
        try:
            return self.remote_store.claim_pairing(pairing_id, secret, device_name)
        except PairingRejected as error:
            raise RemoteValidationError("配对码无效、已过期或已被使用。") from error

    def authenticate(self, token: str) -> dict | None:
        return self.remote_store.authenticate(token)

    def _session(self, session_id: str) -> dict:
        session = self.remote_store.get_synced_session(session_id)
        if session is None:
            raise RemoteNotFound("远程同步会话不存在。")
        return session

    @staticmethod
    def _session_summary(session: dict) -> dict:
        return {
            "id": session["id"],
            "name": session["name"],
            "threadId": session["threadId"],
            "createdAt": session.get("createdAt") or "",
        }

    def list_sessions(self) -> list[dict]:
        return [
            self._session_summary(session)
            for session in self.remote_store.list_synced_sessions()
        ]

    def list_local_sessions(self, limit: int) -> list[dict]:
        try:
            snapshots = self.adapter.list_threads(limit=limit)
        except CodexAdapterError as error:
            raise RemoteApplicationError("Codex 本机会话当前不可读取。") from error
        synced_ids = {
            session["threadId"] for session in self.remote_store.list_synced_sessions()
        }
        return [
            {
                "threadId": item.thread_id,
                "name": item.name,
                "threadStatus": item.thread_status,
                "activeFlags": list(item.active_flags),
                "synced": item.thread_id in synced_ids,
            }
            for item in snapshots
        ]

    def create_synced_session(self, payload: dict) -> dict:
        unknown = set(payload) - {"name", "threadId"}
        if unknown:
            raise RemoteValidationError("远程同步会话包含不支持的字段。")
        try:
            name = validate_required_text(payload.get("name"), "name")
            thread_id = validate_thread_id(payload.get("threadId"))
        except ValidationError as error:
            raise RemoteValidationError(str(error)) from error
        if len(name) > 120:
            raise RemoteValidationError("会话名称不能超过 120 个字符。")
        try:
            session = self.remote_store.create_synced_session(
                name=name, thread_id=thread_id
            )
        except sqlite3.IntegrityError as error:
            raise RemoteValidationError("该 Codex 会话已在远程同步目录中。") from error
        return self._session_summary(session)

    def delete_synced_session(self, session_id: str) -> None:
        session = self._session(session_id)
        if self.approval_broker is not None:
            self.approval_broker.cancel_thread(session["threadId"])
        if not self.remote_store.delete_synced_session(session_id):
            raise RemoteNotFound("远程同步会话不存在。")

    def list_approvals(self) -> list[dict]:
        if self.approval_broker is None:
            return []
        sessions = {
            session["threadId"]: session
            for session in self.remote_store.list_synced_sessions()
        }
        approvals = []
        for approval in self.approval_broker.list_pending():
            session = sessions.get(approval["threadId"])
            if session is None:
                continue
            approvals.append(
                {
                    **approval,
                    "sessionId": session["id"],
                    "sessionName": session["name"],
                }
            )
        return approvals

    def resolve_approval(
        self,
        approval_id: str,
        payload: dict,
        actor: dict,
    ) -> dict:
        unknown = set(payload) - {"decision"}
        if unknown:
            raise RemoteValidationError("授权操作包含不支持的字段。")
        decision = str(payload.get("decision") or "").strip()
        if decision not in {"accept", "acceptForSession", "acceptForTurn", "decline"}:
            raise RemoteValidationError("请选择有效的授权操作。")
        if self.approval_broker is None:
            raise RemoteNotFound("待处理授权不存在或已经结束。")
        try:
            if decision == "acceptForTurn":
                return self.approval_broker.allow_turn(approval_id, actor)
            return self.approval_broker.resolve(approval_id, decision, actor)
        except ApprovalNotFound as error:
            raise RemoteNotFound(str(error)) from error
        except InvalidApprovalDecision as error:
            raise RemoteValidationError(str(error)) from error
        except ApprovalDeliveryError as error:
            raise RemoteApplicationError(str(error)) from error

    def read_session(self, session_id: str, turn_limit: int = 12) -> dict:
        session = self._session(session_id)
        bounded_limit = max(1, min(int(turn_limit), 30))
        try:
            detail = self.adapter.read_thread_detail(
                session["threadId"], turn_limit=bounded_limit
            )
        except CodexAdapterError as error:
            raise RemoteApplicationError("Codex 会话当前不可读取。") from error
        return {**self._session_summary(session), "conversation": detail}

    def send_message(self, session_id: str, payload: dict) -> dict:
        session = self._session(session_id)
        prompt = str(payload.get("message") or "").strip()
        image_url = _validated_image_url(payload.get("image"))
        if len(prompt) > 20_000:
            raise RemoteValidationError("消息不能为空，且不能超过 20000 个字符。")
        if not prompt and not image_url:
            raise RemoteValidationError(
                "\u8bf7\u8f93\u5165\u6d88\u606f\u6216\u6dfb\u52a0\u4e00\u5f20\u622a\u56fe\u3002"
            )
        try:
            result = self.adapter.send_message(
                session["threadId"], prompt, image_url=image_url
            )
        except CodexAdapterError as error:
            raise RemoteApplicationError("消息未能由 Codex App Server 确认发送。") from error
        return {**result, "threadId": session["threadId"]}

    def list_projects(self) -> list[dict]:
        projects = self.project_provider()
        return [
            {
                "id": project.get("id", ""),
                "name": project.get("name", ""),
                "mode": project.get("mode", "local"),
                "state": project.get("state", "unknown"),
                "stateLabel": project.get("stateLabel", "未知"),
            }
            for project in projects
        ]
