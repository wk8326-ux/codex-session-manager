from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .models import SessionSnapshot, TurnSnapshot


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


def _thread_state(status: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(status, dict):
        return "", ()
    status_type = status.get("type")
    normalized_status = status_type if isinstance(status_type, str) else ""
    raw_flags = status.get("activeFlags")
    if not isinstance(raw_flags, list):
        return normalized_status, ()
    return normalized_status, tuple(flag for flag in raw_flags if isinstance(flag, str))


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
    return TurnSnapshot(
        id=turn_id,
        status=status,
        error_message=message if isinstance(message, str) else "",
        error_kind=error_kind,
        http_status=http_status,
    )


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
    latest_turn = _turn_snapshot(turns[-1]) if turns else None
    return SessionSnapshot(
        thread_id=thread_id,
        name=name if isinstance(name, str) else "",
        thread_status=thread_status,
        active_flags=active_flags,
        latest_turn=latest_turn,
    )


class CodexAppServerAdapter:
    def __init__(self, transport: RpcTransport) -> None:
        self._transport = transport

    def read_thread(self, thread_id: str) -> SessionSnapshot:
        response = self._transport.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        if not isinstance(response, dict):
            raise CodexProtocolError("thread/read returned a non-object response")
        snapshot = _session_snapshot(response.get("thread"), require_turns=True)
        if snapshot.thread_id != thread_id:
            raise CodexProtocolError("thread/read returned a different thread id")
        return snapshot

    def list_threads(self, limit: int = 5) -> list[SessionSnapshot]:
        response = self._transport.request("thread/list", {"limit": limit})
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise CodexProtocolError("thread/list returned malformed data")
        return [
            _session_snapshot(thread, require_turns=False)
            for thread in response["data"]
        ]

    def start_turn(self, thread_id: str, prompt: str) -> str:
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
                    "input": [
                        {
                            "type": "text",
                            "text": prompt,
                            "text_elements": [],
                        }
                    ],
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
                        "name": "localhost-project-console",
                        "title": "Local Project Console",
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
        except Exception:
            pass

        if method in SAFE_SERVER_REQUEST_RESULTS:
            result = dict(SAFE_SERVER_REQUEST_RESULTS[method])
            self._write_message({"id": request_id, "result": result})
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
            return
        self._write_message(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending_requests = list(self._pending.values())
            self._pending.clear()
        for pending in pending_requests:
            pending.error = error
            pending.event.set()
