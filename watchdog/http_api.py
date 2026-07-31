from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any


class WatchdogHttpApi:
    """Strict dispatcher for the isolated ``/api/watchdog`` namespace."""

    def __init__(self, service: object) -> None:
        self._service = service

    def dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        payload: object,
    ) -> ApiResponse | None:
        if path != "/api/watchdog" and not path.startswith("/api/watchdog/"):
            return None
        if method == "POST" and path == "/api/watchdog/channels":
            if not isinstance(payload, dict):
                return ApiResponse(400, {"message": "请求正文必须是 JSON 对象。"})
            channel = self._service.create_channel(payload)
            return ApiResponse(201, channel)
        parts = path.strip("/").split("/")
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:3] == ["api", "watchdog", "sessions"]
            and parts[4] == "check"
        ):
            return ApiResponse(200, self._service.check_session(parts[3]))
        return ApiResponse(404, {"message": "Watchdog 接口不存在。"})

# Keep the public import stable while the complete dispatcher lives separately.
from .router import WatchdogRouter
WatchdogHttpApi = WatchdogRouter
