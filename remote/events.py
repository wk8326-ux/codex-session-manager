from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


class RemoteEventHub:
    """Bounded, sanitized App Server event stream for remote-synced sessions."""

    def __init__(self, allowed_thread_ids: Callable[[], set[str]]) -> None:
        self._allowed_thread_ids = allowed_thread_ids
        self._condition = threading.Condition()
        self._events: deque[dict] = deque(maxlen=512)
        self._sequence = 0

    def publish(self, method: str, params: dict) -> None:
        thread_id = _string(params.get("threadId"))
        if not thread_id:
            thread = params.get("thread")
            thread_id = _string(thread.get("id")) if isinstance(thread, dict) else ""
        if not thread_id or thread_id not in self._allowed_thread_ids():
            return
        turn = params.get("turn")
        item = params.get("item")
        turn_id = _string(params.get("turnId"))
        status = _string(params.get("status"))
        if isinstance(turn, dict):
            turn_id = turn_id or _string(turn.get("id"))
            status = status or _string(turn.get("status"))
        if not status and isinstance(item, dict):
            status = _string(item.get("status"))
        with self._condition:
            self._sequence += 1
            self._events.append(
                {
                    "sequence": self._sequence,
                    "method": method,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "status": status,
                    "timestamp": _timestamp(),
                }
            )
            self._condition.notify_all()

    def wait(self, after: int, timeout: float = 25.0) -> dict:
        with self._condition:
            if not any(event["sequence"] > after for event in self._events):
                self._condition.wait(max(0.0, min(timeout, 30.0)))
            events = [event for event in self._events if event["sequence"] > after]
            cursor = events[-1]["sequence"] if events else max(after, self._sequence)
            return {"events": events, "cursor": cursor}
