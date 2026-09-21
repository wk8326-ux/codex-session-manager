from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from diagnostics import report as report_diagnostic

from .models import SessionSnapshot, TurnSnapshot


_MESSAGE_HTTP_STATUS = re.compile(
    r"\b(?:unexpected\s+status|last\s+status|upstream_status)\s*:?\s*"
    r"(?:HTTP\s*)?(429|502|503|504)\b|\bHTTP\s+(429|502|503|504)\b",
    re.IGNORECASE,
)
_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s\"']+|\\\\[^\s\"']+|/(?:Users|home|workspace|private)/[^\s\"']+)")


def _limited_text(value: object, limit: int = 12_000) -> str:
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[:limit] + "\n[内容已截断]"


def _safe_error_text(value: object) -> str:
    return _limited_text(_LOCAL_PATH.sub("[本地路径]", value) if isinstance(value, str) else "", 4_000)


class RpcTransport(Protocol):
    def request(self, method: str, params: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class CodexAdapterError(RuntimeError):
    """Base error for Codex App Server adapter failures."""


class DefiniteSendFailure(CodexAdapterError):
    """The App Server explicitly rejected the request."""


class UncertainSendFailure(CodexAdapterError):
    """The request was written but its outcome could not be confirmed."""


class CodexProtocolError(CodexAdapterError):
    """Raised when the App Server returns an incompatible response."""


def _codex_error(error: object) -> tuple[str, int | None]:
    if not isinstance(error, dict):
        return "", None
    info = error.get("codexErrorInfo")
    if not isinstance(info, dict) or len(info) != 1:
        return "", None
    kind, value = next(iter(info.items()))
    status = value.get("httpStatusCode") if isinstance(value, dict) else None
    return str(kind), status if isinstance(status, int) else None


def _message_http_status(message: object) -> int | None:
    if not isinstance(message, str):
        return None
    match = _MESSAGE_HTTP_STATUS.search(message)
    if match is None:
        return None
    value = next((group for group in match.groups() if group is not None), None)
    return int(value) if value is not None else None


def _thread_state(status: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(status, dict):
        return "", ()
    status_type = status.get("type")
    normalized_status = status_type if isinstance(status_type, str) else ""
    raw_flags = status.get("activeFlags")
    if not isinstance(raw_flags, list):
        return normalized_status, ()
    return normalized_status, tuple(flag for flag in raw_flags if isinstance(flag, str))


def _latest_status_turn(turns: list[object]) -> object | None:
    if not turns:
        return None
    # App Server can append compatibility rollout transcripts after the real
    # turn record. Those transcripts have null lifecycle fields and their
    # synthetic "completed" status must not hide a monitored turn failure.
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        if any(turn.get(field) is not None for field in (
            "startedAt", "completedAt", "durationMs"
        )):
            return turn
    return turns[-1]


def _turn_diagnostic_text(turn: dict) -> str:
    items = turn.get("items")
    if not isinstance(items, list):
        return ""
    messages: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "error":
            continue
        value = item.get("text") or item.get("message")
        if isinstance(value, str) and value.strip():
            messages.append(value.strip())
    return _limited_text("\n".join(messages))


def _epoch_seconds(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _turn_snapshot(turn: object) -> TurnSnapshot:
    if not isinstance(turn, dict):
        raise CodexProtocolError("thread/read returned a malformed turn")
    turn_id = turn.get("id")
    status = turn.get("status")
    if not isinstance(turn_id, str) or not isinstance(status, str):
        raise CodexProtocolError("thread/read returned a turn without a valid id/status")
    error = turn.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    error_kind, http_status = _codex_error(error)
    if http_status is None:
        parsed_status = _message_http_status(message)
        if parsed_status is not None:
            http_status = parsed_status
            if not error_kind:
                error_kind = "messageHttpStatus"
    return TurnSnapshot(
        id=turn_id,
        status=status,
        error_message=message if isinstance(message, str) else "",
        error_kind=error_kind,
        http_status=http_status,
        diagnostic_text=_turn_diagnostic_text(turn),
        started_at=_epoch_seconds(turn.get("startedAt")),
        completed_at=_epoch_seconds(turn.get("completedAt")),
    )


def _recent_error_turn(turns: list[object], latest: TurnSnapshot | None) -> TurnSnapshot | None:
    """Find the newest failed turn that still explains an unfinished session.

    Only the turns *after* the newest success are considered. Once a turn
    completes normally the session has moved past any earlier API failure, so
    resuming from that old error would replay work the user already finished.
    """

    if latest is None or latest.status == "completed":
        return None
    # ``_latest_status_turn`` may skip trailing compatibility rollout records,
    # so locate the real turn by id instead of assuming it is the last element.
    index = next(
        (
            position
            for position, turn in enumerate(turns)
            if isinstance(turn, dict) and turn.get("id") == latest.id
        ),
        None,
    )
    if index is None:
        return None
    for raw_turn in reversed(turns[:index]):
        if not isinstance(raw_turn, dict):
            continue
        try:
            candidate = _turn_snapshot(raw_turn)
        except CodexProtocolError:
            continue
        if candidate.status == "completed" and not candidate.has_error_evidence:
            return None
        if candidate.has_error_evidence:
            return candidate
    return None


def _session_snapshot(thread: object, *, require_turns: bool) -> SessionSnapshot:
    if not isinstance(thread, dict):
        raise CodexProtocolError("App Server response lacks a valid thread")
    thread_id = thread.get("id")
    if not isinstance(thread_id, str):
        raise CodexProtocolError("App Server thread lacks a valid id")
    name = thread.get("name")
    thread_status, active_flags = _thread_state(thread.get("status"))
    turns = thread.get("turns")
    if require_turns and not isinstance(turns, list):
        raise CodexProtocolError("thread/read returned a malformed turns list")
    if turns is not None and not isinstance(turns, list):
        raise CodexProtocolError("App Server returned a malformed turns list")
    latest_turn_data = _latest_status_turn(turns) if isinstance(turns, list) else None
    latest_turn = _turn_snapshot(latest_turn_data) if latest_turn_data is not None else None
    return SessionSnapshot(
        thread_id=thread_id,
        name=name if isinstance(name, str) else "",
        thread_status=thread_status,
        active_flags=active_flags,
        latest_turn=latest_turn,
        recent_error_turn=(
            _recent_error_turn(turns, latest_turn) if isinstance(turns, list) else None
        ),
    )


def _user_input(prompt: str, image_url: str | None = None) -> list[dict]:
    items: list[dict] = []
    if prompt:
        items.append({"type": "text", "text": prompt, "text_elements": []})
    if image_url:
        items.append({"type": "image", "url": image_url})
    return items


def _safe_user_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    image_count = sum(
        1
        for item in content
        if isinstance(item, dict) and item.get("type") in {"image", "localImage"}
    )
    if image_count:
        parts.append(f"\u9644\u5e26 {image_count} \u5f20\u622a\u56fe")
    return "\n".join(part for part in parts if part)


def _safe_thread_item(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    item_type = item.get("type")
    if not isinstance(item_id, str) or not isinstance(item_type, str):
        return None
    safe: dict = {"id": item_id, "type": item_type}
    if item_type == "userMessage":
        safe["text"] = _limited_text(_safe_user_text(item.get("content")))
    elif item_type in {"agentMessage", "plan"}:
        safe["text"] = _limited_text(item.get("text"))
    elif item_type == "reasoning":
        summary = item.get("summary")
        safe["text"] = _limited_text("\n".join(
            value for value in summary or [] if isinstance(value, str)
        ) if isinstance(summary, list) else "")
    elif item_type == "commandExecution":
        safe.update({"label": "命令执行", "status": str(item.get("status") or "")})
    elif item_type == "fileChange":
        changes = item.get("changes")
        safe.update(
            {
                "label": "文件变更",
                "status": str(item.get("status") or ""),
                "count": len(changes) if isinstance(changes, list) else 0,
            }
        )
    elif item_type == "mcpToolCall":
        safe.update(
            {
                "label": "工具调用",
                "server": str(item.get("server") or ""),
                "tool": str(item.get("tool") or ""),
                "status": str(item.get("status") or ""),
            }
        )
    elif item_type == "webSearch":
        safe.update({"label": "网页搜索", "status": str(item.get("status") or "")})
    else:
        safe.update({"label": "任务活动", "status": str(item.get("status") or "")})
    return safe


def _safe_thread_detail(thread: object, *, turn_limit: int) -> dict:
    if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
        raise CodexProtocolError("thread/read returned a malformed thread")
    status, active_flags = _thread_state(thread.get("status"))
    raw_turns = thread.get("turns")
    if not isinstance(raw_turns, list):
        raise CodexProtocolError("thread/read returned a malformed turns list")
    latest_turn_data = _latest_status_turn(raw_turns)
    latest_turn = (
        _turn_snapshot(latest_turn_data) if latest_turn_data is not None else None
    )
    turns = []
    for raw_turn in raw_turns[-turn_limit:]:
        if not isinstance(raw_turn, dict):
            continue
        turn_id = raw_turn.get("id")
        turn_status = raw_turn.get("status")
        if not isinstance(turn_id, str) or not isinstance(turn_status, str):
            continue
        items = [
            safe
            for safe in (_safe_thread_item(item) for item in raw_turn.get("items", []))
            if safe is not None
        ] if isinstance(raw_turn.get("items", []), list) else []
        error = raw_turn.get("error")
        error_message = error.get("message") if isinstance(error, dict) else ""
        turns.append(
            {
                "id": turn_id,
                "status": turn_status,
                "items": items,
                "error": _safe_error_text(error_message),
            }
        )
    return {
        "threadId": thread["id"],
        "name": thread.get("name") if isinstance(thread.get("name"), str) else "",
        "status": status,
        "activeFlags": list(active_flags),
        "latestTurnId": latest_turn.id if latest_turn else "",
        "latestTurnStatus": latest_turn.status if latest_turn else "",
        "latestTurnError": latest_turn.error_message if latest_turn else "",
        "latestTurnHttpStatus": latest_turn.http_status if latest_turn else None,
        "turns": turns,
    }


class CodexAppServerAdapter:
    def __init__(self, transport: RpcTransport) -> None:
        self._transport = transport

    def read_thread(self, thread_id: str) -> SessionSnapshot:
        response = self._read_thread_response(thread_id)
        if not isinstance(response, dict):
            raise CodexProtocolError("thread/read returned a non-object response")
        snapshot = _session_snapshot(response.get("thread"), require_turns=True)
        if snapshot.thread_id != thread_id:
            raise CodexProtocolError("thread/read returned a different thread id")
        return snapshot

    def read_status(self, thread_id: str, *, turn_limit: int = 5) -> SessionSnapshot:
        """Read just enough to classify a session, without loading transcripts.

        ``thread/read`` with ``includeTurns=True`` returns the whole transcript
        and measured 1.6-6.9s on long sessions, which is what pushed every
        status refresh past its deadline. Pairing a metadata-only
        ``thread/read`` (about 1ms) with ``thread/turns/list`` (about 30ms)
        yields the same lifecycle answer for a fraction of the cost, so the
        session list can stay live without making the local process look heavy.
        """

        bounded_limit = max(1, min(int(turn_limit), 20))
        response = self._transport.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        if not isinstance(response, dict):
            raise CodexProtocolError("thread/read returned a non-object response")
        thread = response.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexProtocolError("thread/read returned a different thread id")
        thread_status, active_flags = _thread_state(thread.get("status"))
        name = thread.get("name")

        turns_response = self._transport.request(
            "thread/turns/list", {"threadId": thread_id, "limit": bounded_limit}
        )
        if not isinstance(turns_response, dict):
            raise CodexProtocolError("thread/turns/list returned a non-object response")
        raw_turns = turns_response.get("data")
        if not isinstance(raw_turns, list):
            raise CodexProtocolError("thread/turns/list returned a malformed turns list")
        # ``thread/turns/list`` is newest-first; the shared helpers expect the
        # chronological order that ``thread/read`` produces.
        turns: list[object] = list(reversed(raw_turns))
        latest_turn_data = _latest_status_turn(turns)
        latest_turn = (
            _turn_snapshot(latest_turn_data) if latest_turn_data is not None else None
        )
        return SessionSnapshot(
            thread_id=thread_id,
            name=name if isinstance(name, str) else "",
            thread_status=thread_status,
            active_flags=active_flags,
            latest_turn=latest_turn,
            recent_error_turn=_recent_error_turn(turns, latest_turn),
        )

    def _read_thread_response(self, thread_id: str) -> dict:
        response = self._transport.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        if not isinstance(response, dict):
            raise CodexProtocolError("thread/read returned a non-object response")
        return response

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        response = self._read_thread_response(thread_id)
        detail = _safe_thread_detail(
            response.get("thread"), turn_limit=max(1, min(int(turn_limit), 100))
        )
        if detail["threadId"] != thread_id:
            raise CodexProtocolError("thread/read returned a different thread id")
        return detail

    def list_threads(self, limit: int = 5) -> list[SessionSnapshot]:
        """List recent sessions from the state DB without scanning transcripts.

        ``thread/list`` defaults to ``useStateDbOnly=False``, which makes the App
        Server scan every rollout JSONL to repair thread metadata. On this
        machine that is 232 files / 2.1GB, so ``limit=50`` took 16.1s and still
        returned only 7 distinct sessions because ``limit`` counts scanned rows,
        not sessions. The request then tripped the 10s transport timeout and
        marked the whole runtime disconnected.

        Asking for state-DB-only rows returns the same 50 distinct sessions in
        0.01s, so the list stays live and no longer degrades the connection.
        """

        response = self._transport.request(
            "thread/list", {"limit": limit, "useStateDbOnly": True}
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise CodexProtocolError("thread/list returned malformed data")
        snapshots: list[SessionSnapshot] = []
        seen_thread_ids: set[str] = set()
        for thread in response["data"]:
            snapshot = _session_snapshot(thread, require_turns=False)
            if snapshot.thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(snapshot.thread_id)
            snapshots.append(snapshot)
        return snapshots

    def start_turn(
        self, thread_id: str, prompt: str, image_url: str | None = None
    ) -> str:
        try:
            resumed = self._transport.request(
                "thread/resume", {"threadId": thread_id}
            )
        except (DefiniteSendFailure, CodexProtocolError):
            raise
        except Exception as error:
            raise DefiniteSendFailure(
                "thread/resume failed before turn/start"
            ) from error
        resumed_thread = resumed.get("thread") if isinstance(resumed, dict) else None
        resumed_id = (
            resumed_thread.get("id") if isinstance(resumed_thread, dict) else None
        )
        if resumed_id != thread_id:
            raise CodexProtocolError(
                "thread/resume returned a different or invalid thread id"
            )

        try:
            response = self._transport.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": _user_input(prompt, image_url),
                },
            )
        except (DefiniteSendFailure, UncertainSendFailure, CodexProtocolError):
            raise
        except (TimeoutError, EOFError, ChildProcessError, ConnectionError, OSError) as error:
            raise UncertainSendFailure(
                "turn/start outcome could not be confirmed"
            ) from error
        except Exception as error:
            raise CodexAdapterError("turn/start failed before confirmation") from error
        turn = response.get("turn") if isinstance(response, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise CodexProtocolError("turn/start returned no valid turn id")
        return turn_id

    def send_message(
        self, thread_id: str, prompt: str, image_url: str | None = None
    ) -> dict:
        normalized_prompt = prompt.strip()
        if not normalized_prompt and not image_url:
            raise DefiniteSendFailure("message cannot be empty")
        response = self._read_thread_response(thread_id)
        thread = response.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexProtocolError("thread/read returned a different thread id")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise CodexProtocolError("thread/read returned a malformed turns list")
        latest = turns[-1] if turns else None
        latest_id = latest.get("id") if isinstance(latest, dict) else None
        latest_status = latest.get("status") if isinstance(latest, dict) else None
        if latest_status == "inProgress" and isinstance(latest_id, str):
            try:
                steered = self._transport.request(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": latest_id,
                        "input": _user_input(normalized_prompt, image_url),
                    },
                )
            except (DefiniteSendFailure, UncertainSendFailure, CodexProtocolError):
                raise
            except (TimeoutError, EOFError, ChildProcessError, ConnectionError, OSError) as error:
                raise UncertainSendFailure(
                    "turn/steer outcome could not be confirmed"
                ) from error
            turn_id = steered.get("turnId") if isinstance(steered, dict) else None
            if not isinstance(turn_id, str) or turn_id != latest_id:
                raise CodexProtocolError("turn/steer returned no matching turn id")
            return {"turnId": turn_id, "delivery": "steered"}
        return {
            "turnId": self.start_turn(thread_id, normalized_prompt, image_url),
            "delivery": "started",
        }

    def close(self) -> None:
        self._transport.close()


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


SAFE_SERVER_REQUEST_RESULTS = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
}

ApprovalPolicy = Callable[[str, str], bool]
EventHandler = Callable[[str, dict], None]


class ApprovalQueue(Protocol):
    def offer(
        self,
        method: str,
        params: dict,
        resolver: Callable[[str], None],
    ) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _codex_executable() -> str:
    if os.name == "nt":
        command_shim = shutil.which("codex.cmd")
        if command_shim:
            npm_root = Path(command_shim).parent
            candidates = npm_root.glob(
                "node_modules/@openai/codex/node_modules/@openai/"
                "codex-win32-*/vendor/*/bin/codex.exe"
            )
            for candidate in sorted(candidates):
                if candidate.is_file():
                    return str(candidate)
        return shutil.which("codex.exe") or command_shim or "codex"
    return "codex"


class StdioJsonRpcClient:
    def __init__(
        self,
        *,
        request_timeout: float = 10.0,
        on_attention: Callable[[str, str], None] | None = None,
        approval_policy: ApprovalPolicy | None = None,
        approval_broker: ApprovalQueue | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            [_codex_executable(), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
        )
        self._request_timeout = request_timeout
        self._on_attention = on_attention or (lambda _thread_id, _method: None)
        self._approval_policy = approval_policy or (
            lambda _thread_id, _turn_id: False
        )
        self._approval_broker = approval_broker
        self._on_event = on_event or (lambda _method, _params: None)
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingRequest] = {}
        self._next_id = 1
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop,
            name="codex-app-server-reader",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name="codex-app-server-stderr",
            daemon=True,
        )
        self._stderr_reader.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-session-manager",
                        "title": "Codex Session Manager",
                        "version": "1.0",
                    },
                    "capabilities": None,
                },
            )
            self._write_message({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def request(
        self, method: str, params: dict, *, timeout: float | None = None
    ) -> dict:
        if self._closed:
            raise CodexAdapterError("Codex App Server transport is closed")
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[request_id] = pending
        try:
            self._write_message(
                {"id": request_id, "method": method, "params": params}
            )
        except BaseException as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise UncertainSendFailure(
                f"Codex App Server request write was not confirmed: {method}"
            ) from error

        wait_seconds = self._request_timeout if timeout is None else timeout
        if not pending.event.wait(wait_seconds):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise UncertainSendFailure(
                f"Codex App Server request timed out: {method}"
            )
        if pending.error is not None:
            raise pending.error
        if not isinstance(pending.result, dict):
            raise CodexProtocolError(f"{method} returned a non-object result")
        return pending.result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stdin = self._process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._fail_pending(UncertainSendFailure("Codex App Server transport closed"))
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=2)

    def is_alive(self) -> bool:
        return not self._closed and self._process.poll() is None

    def set_event_handler(self, handler: EventHandler) -> None:
        self._on_event = handler

    def _write_message(self, message: dict) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise RuntimeError("Codex App Server stdin is unavailable")
        serialized = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            stdin.write(serialized + "\n")
            stdin.flush()

    def _read_loop(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._fail_pending(
                UncertainSendFailure("Codex App Server stdout is unavailable")
            )
            return
        try:
            for line in stdout:
                try:
                    message = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if "method" in message and self._numeric_id(message_id):
                    self._handle_server_request(message)
                elif self._numeric_id(message_id):
                    self._handle_response(message)
                elif "method" in message:
                    self._handle_notification(message)
        except BaseException as error:
            self._fail_pending(error)
            return
        self._fail_pending(
            UncertainSendFailure("Codex App Server exited before replying")
        )

    def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for _line in stderr:
                pass
        except (OSError, ValueError):
            return

    @staticmethod
    def _numeric_id(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _handle_response(self, message: dict) -> None:
        request_id = message["id"]
        with self._pending_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        error = message.get("error")
        if error is not None:
            pending.error = DefiniteSendFailure(
                "App Server explicitly rejected the JSON-RPC request"
            )
        else:
            pending.result = message.get("result")
        pending.event.set()

    def _handle_server_request(self, message: dict) -> None:
        request_id = message["id"]
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        thread_id = params.get("threadId")
        try:
            self._on_attention(
                thread_id if isinstance(thread_id, str) else "", method
            )
        except Exception as error:
            report_diagnostic(
                "watchdog.codex_adapter",
                f"attention handler failed for {method}",
                error,
            )

        turn_id = params.get("turnId")
        normalized_turn_id = turn_id if isinstance(turn_id, str) else ""

        if method in SAFE_SERVER_REQUEST_RESULTS:
            auto_approve = False
            try:
                auto_approve = self._approval_policy(
                    thread_id if isinstance(thread_id, str) else "",
                    normalized_turn_id,
                )
            except Exception as error:
                auto_approve = False
                report_diagnostic(
                    "watchdog.codex_adapter",
                    f"approval policy failed for {method}",
                    error,
                )
            if not auto_approve and self._queue_server_approval(
                request_id, method, params
            ):
                return
            decision = self._approval_decision(params) if auto_approve else "decline"
            result = {"decision": decision}
            self._write_message({"id": request_id, "result": result})
            self._emit_event(method, params, watchdog_decision=decision)
            return
        if method == "item/permissions/requestApproval":
            if self._queue_server_approval(request_id, method, params):
                return
            self._write_message(
                {"id": request_id, "result": {"permissions": {}}}
            )
            self._emit_event(method, params, watchdog_decision="decline")
            return
        if method == "item/tool/requestUserInput":
            questions = params.get("questions")
            answers: dict[str, dict[str, list[str]]] = {}
            if isinstance(questions, list):
                for question in questions:
                    question_id = (
                        question.get("id") if isinstance(question, dict) else None
                    )
                    if isinstance(question_id, str):
                        answers[question_id] = {"answers": []}
            self._write_message(
                {"id": request_id, "result": {"answers": answers}}
            )
            self._emit_event(method, params, watchdog_decision="manual")
            return
        self._write_message(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )

    def _queue_server_approval(
        self,
        request_id: int | float,
        method: str,
        params: dict,
    ) -> bool:
        if self._approval_broker is None:
            return False
        captured = dict(params)

        def resolve(decision: str) -> None:
            if method == "item/permissions/requestApproval":
                requested = captured.get("permissions")
                permissions = (
                    requested
                    if decision == "accept" and isinstance(requested, dict)
                    else {}
                )
                result = {"permissions": permissions}
            else:
                result = {"decision": decision}
            self._write_message({"id": request_id, "result": result})
            self._emit_event(method, captured, watchdog_decision=decision)

        try:
            return bool(self._approval_broker.offer(method, captured, resolve))
        except Exception as error:
            report_diagnostic(
                "watchdog.codex_adapter",
                f"approval broker rejected {method}",
                error,
            )
            return False

    @staticmethod
    def _approval_decision(params: dict) -> str:
        raw = params.get("availableDecisions")
        if not isinstance(raw, list):
            return "acceptForSession"
        available = {item for item in raw if isinstance(item, str)}
        if "acceptForSession" in available:
            return "acceptForSession"
        if "accept" in available:
            return "accept"
        return "decline"

    def _handle_notification(self, message: dict) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        self._emit_event(method, params if isinstance(params, dict) else {})

    def _emit_event(
        self, method: str, params: dict, *, watchdog_decision: str | None = None
    ) -> None:
        event_params = dict(params)
        if watchdog_decision is not None:
            event_params["watchdogDecision"] = watchdog_decision
        try:
            self._on_event(method, event_params)
        except Exception as error:
            report_diagnostic(
                "watchdog.codex_adapter",
                f"event handler failed for {method}",
                error,
            )

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending_requests = list(self._pending.values())
            self._pending.clear()
        for pending in pending_requests:
            pending.error = error
            pending.event.set()
