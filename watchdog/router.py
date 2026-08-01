from __future__ import annotations

from datetime import datetime
from typing import Any

from .application import ResourceConflictError, ResourceNotFoundError
from .codex_adapter import CodexAdapterError
from .http_api import ApiResponse
from .store import ChannelInUseError, WatchdogStoreError
from .validation import ValidationError


class WatchdogRouter:
    """Dispatch the complete watchdog HTTP API without touching project state."""

    _RUN_FILTERS = {"sessionId", "channelId", "decision", "from", "to", "limit"}

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
        try:
            return self._dispatch(method.upper(), path, query, payload)
        except ResourceNotFoundError as error:
            return ApiResponse(404, {"message": str(error)})
        except (ResourceConflictError, ChannelInUseError, WatchdogStoreError) as error:
            return ApiResponse(409, {"message": str(error)})
        except (ValidationError, TypeError, ValueError) as error:
            return ApiResponse(400, {"message": str(error)})
        except CodexAdapterError:
            return ApiResponse(503, {"message": "Codex connection is unavailable."})
        except Exception:
            return ApiResponse(500, {"message": "Watchdog request failed."})

    def _dispatch(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        payload: object,
    ) -> ApiResponse:
        if path == "/api/watchdog/status":
            return self._method(method, {"GET": lambda: self._service.get_status()})
        if path == "/api/watchdog/settings":
            return self._method(
                method,
                {
                    "GET": lambda: self._service.get_settings(),
                    "PUT": lambda: self._service.update_settings(self._body(payload)),
                },
            )
        if path == "/api/watchdog/channels":
            return self._method(
                method,
                {
                    "GET": lambda: self._service.list_channels(),
                    "POST": lambda: self._service.create_channel(self._body(payload)),
                },
                created_method="POST",
            )
        if path == "/api/watchdog/sessions":
            return self._method(
                method,
                {
                    "GET": lambda: self._service.list_sessions(),
                    "POST": lambda: self._service.create_session(self._body(payload)),
                },
                created_method="POST",
            )
        if path == "/api/watchdog/local-codex-sessions":
            limit = self._bounded_query_int(query, "limit", 20, 1, 100)
            self._reject_query(query, {"limit"})
            return self._method(
                method, {"GET": lambda: self._service.list_local_sessions(limit)}
            )
        if path == "/api/watchdog/runs":
            filters = self._run_filters(query)
            return self._method(
                method, {"GET": lambda: self._service.list_runs(filters)}
            )
        if path == "/api/watchdog/bridge/status":
            return self._method(
                method,
                {"GET": lambda: self._service.get_desktop_bridge_status()},
            )
        if path == "/api/watchdog/bridge/jobs/claim":
            return self._method(
                method,
                {
                    "POST": lambda: self._service.claim_desktop_bridge_job(
                        self._body(payload)
                    )
                },
            )

        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[:3] == ["api", "watchdog", "channels"]:
            channel_id = parts[3]
            if len(parts) == 4:
                return self._method(
                    method,
                    {
                        "GET": lambda: self._service.get_channel(channel_id),
                        "PUT": lambda: self._service.update_channel(
                            channel_id, self._body(payload)
                        ),
                        "DELETE": lambda: self._delete_channel(channel_id),
                    },
                )
            if len(parts) == 5 and parts[4] == "probe":
                return self._method(
                    method,
                    {"POST": lambda: self._service.probe_channel(channel_id)},
                )
        if len(parts) >= 4 and parts[:3] == ["api", "watchdog", "sessions"]:
            session_id = parts[3]
            if len(parts) == 4:
                return self._method(
                    method,
                    {
                        "GET": lambda: self._service.get_session(session_id),
                        "PUT": lambda: self._service.update_session(
                            session_id, self._body(payload)
                        ),
                        "DELETE": lambda: self._delete_session(session_id),
                    },
                )
            if len(parts) == 5 and parts[4] == "check":
                return self._method(
                    method,
                    {"POST": lambda: self._service.check_session(session_id)},
                )
        if (
            len(parts) == 6
            and parts[:4] == ["api", "watchdog", "bridge", "jobs"]
        ):
            job_id = parts[4]
            if parts[5] == "started":
                return self._method(
                    method,
                    {
                        "POST": lambda: self._service.mark_desktop_bridge_started(
                            job_id, self._body(payload)
                        )
                    },
                )
            if parts[5] == "finish":
                return self._method(
                    method,
                    {
                        "POST": lambda: self._service.finish_desktop_bridge_job(
                            job_id, self._body(payload)
                        )
                    },
                )
        return ApiResponse(404, {"message": "Watchdog route not found."})

    @staticmethod
    def _body(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object.")
        return payload

    @staticmethod
    def _method(
        method: str,
        handlers: dict[str, Any],
        *,
        created_method: str | None = None,
    ) -> ApiResponse:
        handler = handlers.get(method)
        if handler is None:
            return ApiResponse(405, {"message": "Method not allowed."})
        body = handler()
        status = 201 if method == created_method else 200
        return ApiResponse(204 if body is None else status, body)

    def _delete_channel(self, channel_id: str) -> None:
        self._service.delete_channel(channel_id)

    def _delete_session(self, session_id: str) -> None:
        self._service.delete_session(session_id)

    @staticmethod
    def _single_query(
        query: dict[str, list[str]], key: str, default: str | None = None
    ) -> str | None:
        values = query.get(key)
        if values is None:
            return default
        if not isinstance(values, list) or len(values) != 1:
            raise ValidationError(f"{key} must appear once")
        return values[0]

    def _bounded_query_int(
        self,
        query: dict[str, list[str]],
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = self._single_query(query, key, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"{key} must be an integer") from None
        if not minimum <= value <= maximum:
            raise ValidationError(f"{key} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _reject_query(query: dict[str, list[str]], allowed: set[str]) -> None:
        unknown = set(query) - allowed
        if unknown:
            raise ValidationError(f"unknown query field: {sorted(unknown)[0]}")

    def _run_filters(self, query: dict[str, list[str]]) -> dict:
        self._reject_query(query, self._RUN_FILTERS)
        result: dict[str, object] = {}
        for key in ("sessionId", "channelId", "decision", "from", "to"):
            value = self._single_query(query, key)
            if value:
                result[key] = value
        result["limit"] = self._bounded_query_int(query, "limit", 100, 1, 500)
        for key in ("from", "to"):
            if key in result:
                try:
                    datetime.strptime(str(result[key]), "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    raise ValidationError(f"{key} must be a UTC timestamp") from None
        if result.get("from") and result.get("to") and result["from"] > result["to"]:
            raise ValidationError("from must not be later than to")
        return result
