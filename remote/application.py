from __future__ import annotations

from urllib.parse import urlparse

from watchdog.codex_adapter import CodexAdapterError

from .events import RemoteEventHub
from .store import PairingRejected, RemoteStore


class RemoteApplicationError(RuntimeError):
    pass


class RemoteNotFound(RemoteApplicationError):
    pass


class RemoteValidationError(RemoteApplicationError):
    pass


class RemoteApplication:
    def __init__(
        self,
        remote_store: RemoteStore,
        session_store: object,
        adapter: object,
        event_hub: RemoteEventHub,
        project_provider,
        *,
        default_base_url: str,
        codex_connected: bool,
    ) -> None:
        self.remote_store = remote_store
        self.session_store = session_store
        self.adapter = adapter
        self.event_hub = event_hub
        self.project_provider = project_provider
        self.default_base_url = default_base_url
        self.codex_connected = codex_connected

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
        return {
            "enabled": True,
            "codexConnected": self.codex_connected,
            "baseUrl": base_url,
            "deviceCount": len(self.remote_store.list_devices()),
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
        session = self.session_store.get_session(session_id)
        if session is None:
            raise RemoteNotFound("监控会话不存在。")
        return session

    @staticmethod
    def _session_summary(session: dict) -> dict:
        return {
            "id": session["id"],
            "name": session["name"],
            "threadId": session["threadId"],
            "monitoringEnabled": bool(session.get("enabled")),
            "lastSessionState": session.get("lastSessionState") or "unknown",
            "lastCheckResult": session.get("lastCheckResult") or "",
            "lastCheckedAt": session.get("lastCheckedAt") or "",
        }

    def list_sessions(self) -> list[dict]:
        return [self._session_summary(session) for session in self.session_store.list_sessions()]

    def read_session(self, session_id: str) -> dict:
        session = self._session(session_id)
        try:
            detail = self.adapter.read_thread_detail(session["threadId"], turn_limit=12)
        except CodexAdapterError as error:
            raise RemoteApplicationError("Codex 会话当前不可读取。") from error
        return {**self._session_summary(session), "conversation": detail}

    def send_message(self, session_id: str, payload: dict) -> dict:
        session = self._session(session_id)
        prompt = str(payload.get("message") or "").strip()
        if not prompt or len(prompt) > 20_000:
            raise RemoteValidationError("消息不能为空，且不能超过 20000 个字符。")
        try:
            result = self.adapter.send_message(session["threadId"], prompt)
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
