"""Cached, coalesced remote views of Codex sessions."""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from watchdog.codex_adapter import CodexAdapterError


@dataclass
class _ProjectionEntry:
    detail: dict
    fetched_at: float
    revision: int
    turn_limit: int


@dataclass
class _StatusEntry:
    status: dict
    fetched_at: float


class SessionProjection:
    """Hide App Server reads behind cached revisions and request coalescing."""

    def __init__(
        self,
        adapter: object,
        *,
        fresh_seconds: float = 1.5,
        generation_provider: Callable[[], int] | None = None,
        max_workers: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._fresh_seconds = max(0.0, fresh_seconds)
        self._generation_provider = generation_provider or (lambda: 0)
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, _ProjectionEntry] = {}
        self._statuses: dict[str, _StatusEntry] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._revision = 0
        self._generation = self._generation_provider()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="session-projection",
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def invalidate(self, thread_id: str) -> None:
        if not thread_id:
            return
        with self._lock:
            self._revision += 1
            entry = self._entries.get(thread_id)
            if entry is not None:
                entry.fetched_at = 0.0
                entry.revision = self._revision
            status = self._statuses.get(thread_id)
            if status is not None:
                status.fetched_at = 0.0

    def read(self, thread_id: str, *, turn_limit: int = 6, force: bool = False) -> dict:
        bounded_limit = max(1, min(int(turn_limit), 30))
        self._refresh_generation()
        while True:
            with self._lock:
                entry = self._entries.get(thread_id)
                if (
                    not force
                    and entry is not None
                    and entry.turn_limit >= bounded_limit
                    and self._clock() - entry.fetched_at <= self._fresh_seconds
                ):
                    return self._limited(entry.detail, bounded_limit)
                event = self._inflight.get(thread_id)
                if event is None:
                    event = threading.Event()
                    self._inflight[thread_id] = event
                    owner = True
                else:
                    owner = False
            if owner:
                break
            if not event.wait(12):
                raise CodexAdapterError("Codex session projection timed out")
            force = False

        try:
            detail = self._adapter.read_thread_detail(
                thread_id, turn_limit=bounded_limit
            )
            with self._lock:
                self._revision += 1
                self._entries[thread_id] = _ProjectionEntry(
                    copy.deepcopy(detail),
                    self._clock(),
                    self._revision,
                    bounded_limit,
                )
            return self._limited(detail, bounded_limit)
        finally:
            with self._lock:
                completed = self._inflight.pop(thread_id, None)
                if completed is not None:
                    completed.set()

    def list_statuses(self, thread_ids: list[str]) -> dict[str, dict | None]:
        unique_ids = list(dict.fromkeys(thread_ids))

        def read_status(thread_id: str) -> tuple[str, dict | None]:
            with self._lock:
                cached = self._statuses.get(thread_id)
                if (
                    cached is not None
                    and self._clock() - cached.fetched_at <= self._fresh_seconds
                ):
                    return thread_id, copy.deepcopy(cached.status)
            try:
                snapshot = self._adapter.read_thread(thread_id)
            except (CodexAdapterError, AttributeError):
                return thread_id, None
            status = self._snapshot_status(snapshot)
            with self._lock:
                self._statuses[thread_id] = _StatusEntry(status, self._clock())
            return thread_id, copy.deepcopy(status)

        futures = [self._executor.submit(read_status, thread_id) for thread_id in unique_ids]
        return dict(future.result() for future in futures)

    def revision(self, thread_id: str) -> int:
        with self._lock:
            entry = self._entries.get(thread_id)
            return entry.revision if entry is not None else 0

    def _refresh_generation(self) -> None:
        current = self._generation_provider()
        with self._lock:
            if current == self._generation:
                return
            self._generation = current
            self._entries.clear()
            self._statuses.clear()
            self._revision += 1

    @staticmethod
    def _limited(detail: dict, turn_limit: int) -> dict:
        result = copy.deepcopy(detail)
        turns = result.get("turns")
        if isinstance(turns, list):
            result["turns"] = turns[-turn_limit:]
        return result

    @staticmethod
    def _snapshot_status(snapshot: object) -> dict:
        latest = getattr(snapshot, "latest_turn", None)
        return {
            "statusKnown": True,
            "threadStatus": getattr(snapshot, "thread_status", ""),
            "activeFlags": list(getattr(snapshot, "active_flags", ())),
            "latestTurnStatus": getattr(latest, "status", "") if latest else "",
            "latestTurnHasError": bool(
                latest and getattr(latest, "error_message", "")
            ),
            "latestTurnHttpStatus": (
                getattr(latest, "http_status", None) if latest else None
            ),
        }
