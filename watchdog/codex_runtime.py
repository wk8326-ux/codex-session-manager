"""Supervise the Codex App Server behind a stable session adapter interface."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from diagnostics import report as report_diagnostic

from .codex_adapter import (
    CodexAdapterError,
    CodexAppServerAdapter,
    CodexProtocolError,
    DefiniteSendFailure,
    StdioJsonRpcClient,
    UncertainSendFailure,
)


class CodexRuntime:
    """Own one replaceable App Server process while callers keep one adapter."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., object] = StdioJsonRpcClient,
        approval_policy=None,
        approval_broker=None,
        reconnect_delays: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
        health_poll_seconds: float = 0.5,
    ) -> None:
        self._client_factory = client_factory
        self._approval_policy = approval_policy
        self._approval_broker = approval_broker
        self._reconnect_delays = reconnect_delays or (1.0,)
        self._health_poll_seconds = max(0.05, health_poll_seconds)
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: object | None = None
        self._adapter: CodexAppServerAdapter | None = None
        self._event_handler: Callable[[str, dict], None] = lambda _method, _params: None
        self._generation = 0
        self._connecting = False
        self._last_error = ""
        self._next_retry_at = 0.0

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="codex-runtime-supervisor",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._disconnect()
        broker = self._approval_broker
        if broker is not None:
            try:
                broker.close()
            except Exception as error:
                report_diagnostic(
                    "watchdog.codex_runtime",
                    "failed to close the approval broker",
                    error,
                )

    def set_event_handler(self, handler: Callable[[str, dict], None]) -> None:
        with self._condition:
            self._event_handler = handler
            client = self._client
        if client is not None:
            client.set_event_handler(handler)

    def is_connected(self) -> bool:
        with self._condition:
            return self._adapter is not None and self._client_alive(self._client)

    def status(self) -> dict:
        with self._condition:
            connected = self._adapter is not None and self._client_alive(self._client)
            retry_in = max(0.0, self._next_retry_at - time.monotonic())
            return {
                "connected": connected,
                "connecting": self._connecting,
                "generation": self._generation,
                "lastError": self._last_error,
                "retryInMs": round(retry_in * 1000) if not connected else 0,
            }

    def ensure_running(self, timeout: float = 10.0) -> None:
        self.start()
        deadline = time.monotonic() + max(0.0, timeout)
        self._wake_event.set()
        with self._condition:
            while not (self._adapter is not None and self._client_alive(self._client)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f": {self._last_error}" if self._last_error else ""
                    raise CodexAdapterError(f"Codex App Server is unavailable{detail}")
                self._condition.wait(min(remaining, 0.25))

    def read_thread(self, thread_id: str):
        return self._call("read_thread", thread_id)

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        return self._call("read_thread_detail", thread_id, turn_limit=turn_limit)

    def list_threads(self, limit: int = 5):
        return self._call("list_threads", limit=limit)

    def start_turn(self, thread_id: str, prompt: str, image_url: str | None = None) -> str:
        return self._call("start_turn", thread_id, prompt, image_url=image_url)

    def send_message(
        self, thread_id: str, prompt: str, image_url: str | None = None
    ) -> dict:
        return self._call("send_message", thread_id, prompt, image_url=image_url)

    def _call(self, method: str, *args, **kwargs):
        self.ensure_running()
        with self._condition:
            adapter = self._adapter
        if adapter is None:
            raise CodexAdapterError("Codex App Server is unavailable")
        try:
            return getattr(adapter, method)(*args, **kwargs)
        except (DefiniteSendFailure, CodexProtocolError):
            raise
        except (UncertainSendFailure, CodexAdapterError):
            self._mark_disconnected()
            raise

    def _run(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            if self.is_connected():
                self._wake_event.wait(self._health_poll_seconds)
                self._wake_event.clear()
                if not self.is_connected():
                    self._disconnect()
                continue
            delay = 0.0 if failures == 0 else self._reconnect_delays[
                min(failures - 1, len(self._reconnect_delays) - 1)
            ]
            with self._condition:
                self._next_retry_at = time.monotonic() + delay
            if self._stop_event.wait(delay):
                break
            if self._connect():
                failures = 0
            else:
                failures += 1

    def _connect(self) -> bool:
        with self._condition:
            if self._adapter is not None and self._client_alive(self._client):
                return True
            stale_client = self._client
            self._client = None
            self._adapter = None
            self._connecting = True
            self._condition.notify_all()
        if stale_client is not None:
            try:
                stale_client.close()
            except Exception as error:
                report_diagnostic(
                    "watchdog.codex_runtime",
                    "failed to close a stale App Server client",
                    error,
                )
        client = None
        try:
            client = self._client_factory(
                approval_policy=self._approval_policy,
                approval_broker=self._approval_broker,
            )
            client.set_event_handler(self._event_handler)
            adapter = CodexAppServerAdapter(client)
        except Exception as error:
            if client is not None:
                try:
                    client.close()
                except Exception as close_error:
                    report_diagnostic(
                        "watchdog.codex_runtime",
                        "failed to close a partially connected App Server client",
                        close_error,
                    )
            with self._condition:
                self._connecting = False
                self._last_error = f"{type(error).__name__}: {error}"
                self._condition.notify_all()
            return False
        with self._condition:
            self._client = client
            self._adapter = adapter
            self._generation += 1
            self._connecting = False
            self._last_error = ""
            self._next_retry_at = 0.0
            self._condition.notify_all()
        return True

    def _mark_disconnected(self) -> None:
        self._disconnect()
        self._wake_event.set()

    def _disconnect(self) -> None:
        with self._condition:
            client = self._client
            self._client = None
            self._adapter = None
            self._condition.notify_all()
        if client is not None:
            try:
                client.close()
            except Exception as error:
                report_diagnostic(
                    "watchdog.codex_runtime",
                    "failed to close the App Server client",
                    error,
                )

    @staticmethod
    def _client_alive(client: object | None) -> bool:
        if client is None:
            return False
        checker = getattr(client, "is_alive", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True
