from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

from .application import (
    RemoteApplication,
    RemoteApplicationError,
    RemoteNotFound,
    RemoteValidationError,
)


@dataclass(frozen=True)
class RemoteResponse:
    status: int
    body: object


def _bearer(headers: dict[str, str]) -> str:
    value = headers.get("Authorization", "")
    prefix = "Bearer "
    return value[len(prefix):].strip() if value.startswith(prefix) else ""


def _turn_limit(parsed) -> int:
    raw = parse_qs(parsed.query).get("turnLimit", ["12"])[0]
    return max(1, min(30, int(raw)))


class AdminRemoteApi:
    def __init__(self, application: RemoteApplication) -> None:
        self.application = application

    def dispatch(self, method: str, path: str, payload: object) -> RemoteResponse | None:
        try:
            parsed = urlparse(path)
            route = parsed.path
            if method == "GET" and route == "/api/remote/status":
                return RemoteResponse(HTTPStatus.OK, self.application.admin_status())
            if method == "POST" and route == "/api/remote/pairings":
                return RemoteResponse(
                    HTTPStatus.CREATED,
                    self.application.create_pairing(payload if isinstance(payload, dict) else {}),
                )
            if method == "GET" and route == "/api/remote/devices":
                return RemoteResponse(HTTPStatus.OK, self.application.list_devices())
            prefix = "/api/remote/devices/"
            if method == "DELETE" and route.startswith(prefix):
                self.application.revoke_device(route[len(prefix):])
                return RemoteResponse(HTTPStatus.NO_CONTENT, {})
            if method == "GET" and route == "/api/remote/sessions":
                return RemoteResponse(HTTPStatus.OK, self.application.list_sessions())
            if method == "GET" and route == "/api/remote/local-sessions":
                query = parse_qs(parsed.query)
                limit = max(1, min(100, int(query.get("limit", ["50"])[0])))
                return RemoteResponse(
                    HTTPStatus.OK, self.application.list_local_sessions(limit)
                )
            if route == "/api/remote/synced-sessions":
                if method == "GET":
                    return RemoteResponse(
                        HTTPStatus.OK, self.application.list_sessions()
                    )
                if method == "POST":
                    return RemoteResponse(
                        HTTPStatus.CREATED,
                        self.application.create_synced_session(
                            payload if isinstance(payload, dict) else {}
                        ),
                    )
            synced_prefix = "/api/remote/synced-sessions/"
            if method == "DELETE" and route.startswith(synced_prefix):
                self.application.delete_synced_session(route[len(synced_prefix):])
                return RemoteResponse(HTTPStatus.NO_CONTENT, {})
            session_prefix = "/api/remote/sessions/"
            if route.startswith(session_prefix):
                remainder = route[len(session_prefix):]
                if method == "POST" and remainder.endswith("/messages"):
                    return RemoteResponse(
                        HTTPStatus.ACCEPTED,
                        self.application.send_message(
                            remainder[:-len("/messages")],
                            payload if isinstance(payload, dict) else {},
                        ),
                    )
                if method == "GET" and "/" not in remainder:
                    return RemoteResponse(
                        HTTPStatus.OK,
                        self.application.read_session(remainder, _turn_limit(parsed)),
                    )
            if method == "GET" and route == "/api/remote/projects":
                return RemoteResponse(HTTPStatus.OK, self.application.list_projects())
            if method == "GET" and route == "/api/remote/events":
                query = parse_qs(parsed.query)
                after = max(0, int(query.get("after", ["0"])[0]))
                timeout = float(query.get("timeout", ["25"])[0])
                return RemoteResponse(
                    HTTPStatus.OK, self.application.event_hub.wait(after, timeout)
                )
        except RemoteValidationError as error:
            return RemoteResponse(HTTPStatus.BAD_REQUEST, {"message": str(error)})
        except RemoteNotFound as error:
            return RemoteResponse(HTTPStatus.NOT_FOUND, {"message": str(error)})
        except (TypeError, ValueError):
            return RemoteResponse(HTTPStatus.BAD_REQUEST, {"message": "事件游标或等待时间无效。"})
        except RemoteApplicationError as error:
            return RemoteResponse(HTTPStatus.BAD_GATEWAY, {"message": str(error)})
        return None


class RemoteHttpApi:
    def __init__(self, application: RemoteApplication) -> None:
        self.application = application

    def dispatch(
        self,
        method: str,
        raw_path: str,
        headers: dict[str, str],
        payload: object,
    ) -> RemoteResponse | None:
        parsed = urlparse(raw_path)
        path = parsed.path
        try:
            pairing_prefix = "/api/remote/pairings/"
            if method == "POST" and path.startswith(pairing_prefix) and path.endswith("/claim"):
                pairing_id = path[len(pairing_prefix):-len("/claim")]
                return RemoteResponse(
                    HTTPStatus.CREATED,
                    self.application.claim_pairing(
                        pairing_id, payload if isinstance(payload, dict) else {}
                    ),
                )

            device = self.application.authenticate(_bearer(headers))
            if device is None:
                return RemoteResponse(HTTPStatus.UNAUTHORIZED, {"message": "设备未配对或授权已撤销。"})
            if method == "GET" and path == "/api/remote/sessions":
                return RemoteResponse(HTTPStatus.OK, self.application.list_sessions())
            session_prefix = "/api/remote/sessions/"
            if path.startswith(session_prefix):
                remainder = path[len(session_prefix):]
                if method == "POST" and remainder.endswith("/messages"):
                    session_id = remainder[:-len("/messages")]
                    return RemoteResponse(
                        HTTPStatus.ACCEPTED,
                        self.application.send_message(
                            session_id, payload if isinstance(payload, dict) else {}
                        ),
                    )
                if method == "GET" and "/" not in remainder:
                    return RemoteResponse(
                        HTTPStatus.OK,
                        self.application.read_session(remainder, _turn_limit(parsed)),
                    )
            if method == "GET" and path == "/api/remote/projects":
                return RemoteResponse(HTTPStatus.OK, self.application.list_projects())
            if method == "GET" and path == "/api/remote/events":
                query = parse_qs(parsed.query)
                try:
                    after = max(0, int(query.get("after", ["0"])[0]))
                    timeout = float(query.get("timeout", ["25"])[0])
                except (TypeError, ValueError):
                    raise RemoteValidationError("事件游标或等待时间无效。")
                return RemoteResponse(
                    HTTPStatus.OK, self.application.event_hub.wait(after, timeout)
                )
        except RemoteValidationError as error:
            return RemoteResponse(HTTPStatus.BAD_REQUEST, {"message": str(error)})
        except RemoteNotFound as error:
            return RemoteResponse(HTTPStatus.NOT_FOUND, {"message": str(error)})
        except RemoteApplicationError as error:
            return RemoteResponse(HTTPStatus.BAD_GATEWAY, {"message": str(error)})
        return None
