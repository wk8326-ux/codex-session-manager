from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4


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
        self._stream_id = str(uuid4())

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
        item_id = ""
        item_type = ""
        status = _string(params.get("status"))
        if isinstance(turn, dict):
            turn_id = turn_id or _string(turn.get("id"))
            status = status or _string(turn.get("status"))
        if isinstance(item, dict):
            item_id = _string(item.get("id"))
            item_type = _string(item.get("type"))
            status = status or _string(item.get("status"))
        with self._condition:
            self._sequence += 1
            self._events.append(
                {
                    "sequence": self._sequence,
                    "method": method,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": item_id,
                    "itemType": item_type,
                    "status": status,
                    "timestamp": _timestamp(),
                }
            )
            self._condition.notify_all()

    def wait(self, after: int, timeout: float = 25.0) -> dict:
        with self._condition:
            if after > self._sequence:
                return {
                    "events": [],
                    "cursor": self._sequence,
                    "streamId": self._stream_id,
                    "resyncRequired": True,
                }
            if not any(event["sequence"] > after for event in self._events):
                self._condition.wait(max(0.0, min(timeout, 30.0)))
            oldest = self._events[0]["sequence"] if self._events else self._sequence + 1
            resync_required = after > 0 and oldest > after + 1
            events = [event for event in self._events if event["sequence"] > after]
            cursor = events[-1]["sequence"] if events else max(after, self._sequence)
            return {
                "events": events,
                "cursor": cursor,
                "streamId": self._stream_id,
                "resyncRequired": resync_required,
            }
