from __future__ import annotations

import time
import unittest

from watchdog.codex_adapter import UncertainSendFailure
from watchdog.codex_runtime import CodexRuntime


class FakeClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.alive = True
        self.closed = False
        self.handler = None
        self.calls = []

    def is_alive(self) -> bool:
        return self.alive and not self.closed

    def set_event_handler(self, handler) -> None:
        self.handler = handler

    def request(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if not self.is_alive():
            raise UncertainSendFailure("transport exited")
        if method == "thread/list":
            return {"data": []}
        raise AssertionError(method)

    def close(self) -> None:
        self.closed = True


class CodexRuntimeTests(unittest.TestCase):
    def test_reconnects_after_child_exit_and_keeps_stable_interface(self) -> None:
        clients: list[FakeClient] = []

        def factory(**_kwargs):
            client = FakeClient(f"client-{len(clients) + 1}")
            clients.append(client)
            return client

        runtime = CodexRuntime(
            client_factory=factory,
            reconnect_delays=(0.01,),
            health_poll_seconds=0.01,
        )
        try:
            runtime.ensure_running(timeout=1)
            self.assertEqual(runtime.list_threads(), [])
            self.assertEqual(runtime.status()["generation"], 1)

            clients[0].alive = False
            deadline = time.monotonic() + 1
            while runtime.status()["generation"] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertTrue(runtime.is_connected())
            self.assertEqual(runtime.status()["generation"], 2)
            self.assertEqual(runtime.list_threads(), [])
        finally:
            runtime.close()

    def test_start_is_non_blocking_when_factory_fails(self) -> None:
        attempts = 0

        def factory(**_kwargs):
            nonlocal attempts
            attempts += 1
            raise OSError("codex missing")

        runtime = CodexRuntime(
            client_factory=factory,
            reconnect_delays=(0.01,),
            health_poll_seconds=0.01,
        )
        started = time.monotonic()
        runtime.start()
        elapsed = time.monotonic() - started
        try:
            deadline = time.monotonic() + 1
            while attempts == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertLess(elapsed, 0.1)
            self.assertFalse(runtime.is_connected())
            self.assertIn("codex missing", runtime.status()["lastError"])
        finally:
            runtime.close()

    def test_event_handler_is_reinstalled_after_restart(self) -> None:
        clients: list[FakeClient] = []
        events = []

        def factory(**_kwargs):
            client = FakeClient(str(len(clients)))
            clients.append(client)
            return client

        runtime = CodexRuntime(
            client_factory=factory,
            reconnect_delays=(0.01,),
            health_poll_seconds=0.01,
        )
        runtime.set_event_handler(lambda method, params: events.append((method, params)))
        try:
            runtime.ensure_running(timeout=1)
            clients[0].handler("turn/started", {"threadId": "one"})
            clients[0].alive = False
            deadline = time.monotonic() + 1
            while len(clients) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            clients[1].handler("turn/completed", {"threadId": "one"})
            self.assertEqual([item[0] for item in events], ["turn/started", "turn/completed"])
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
