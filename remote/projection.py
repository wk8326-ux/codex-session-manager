"""Cached, coalesced remote views of Codex sessions."""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
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
    """Hide App Server reads behind bounded caches and request coalescing."""

    def __init__(
        self,
        adapter: object,
        *,
        fresh_seconds: float = 1.5,
        status_fresh_seconds: float = 60.0,
        max_sessions: int = 24,
        max_status_entries: int = 96,
        generation_provider: Callable[[], int] | None = None,
        max_workers: int = 4,
        wait_seconds: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._fresh_seconds = max(0.0, fresh_seconds)
        # Session-list statuses change far less often than conversation
        # transcripts, and the PWA already receives live event overrides for
        # synced threads. Keeping statuses longer avoids re-reading every
        # transcript on every heartbeat, which is what made the local process
        # look heavy and pushed reads past their deadline.
        self._status_fresh_seconds = max(0.0, float(status_fresh_seconds))
        self._max_sessions = max(1, int(max_sessions))
        self._max_status_entries = max(1, int(max_status_entries))
        # A concurrent reader must never block a request forever: the PWA polls
        # frequently, so serving a slightly older view beats stalling. The
        # budget still has to cover one real App Server read, which measured
        # 3-3.5s for a session with a long transcript; a shorter bound made
        # every status refresh time out and discard its result.
        self._wait_seconds = max(0.1, float(wait_seconds))
        self._generation_provider = generation_provider or (lambda: 0)
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _ProjectionEntry] = OrderedDict()
        self._statuses: OrderedDict[str, _StatusEntry] = OrderedDict()
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
                self._entries.move_to_end(thread_id)
            status = self._statuses.get(thread_id)
            if status is not None:
                status.fetched_at = 0.0
                self._statuses.move_to_end(thread_id)

    def read(self, thread_id: str, *, turn_limit: int = 6, force: bool = False) -> dict:
        bounded_limit = max(1, min(int(turn_limit), 30))
        self._refresh_generation()
        deadline = self._clock() + self._wait_seconds
        while True:
            stale: dict | None = None
            with self._lock:
                entry = self._entries.get(thread_id)
                if entry is not None:
                    if (
                        not force
                        and entry.turn_limit >= bounded_limit
                        and self._clock() - entry.fetched_at <= self._fresh_seconds
                    ):
                        self._entries.move_to_end(thread_id)
                        return self._limited(entry.detail, bounded_limit)
                    stale = entry.detail
                event = self._inflight.get(thread_id)
                if event is None:
                    event = threading.Event()
                    self._inflight[thread_id] = event
                    owner = True
                else:
                    owner = False
            if owner:
                break
            remaining = deadline - self._clock()
            if remaining > 0:
                event.wait(remaining)
            with self._lock:
                entry = self._entries.get(thread_id)
                if (
                    entry is not None
                    and entry.turn_limit >= bounded_limit
                    and self._clock() - entry.fetched_at <= self._fresh_seconds
                ):
                    self._entries.move_to_end(thread_id)
                    return self._limited(entry.detail, bounded_limit)
            if self._clock() >= deadline:
                if stale is not None:
                    return self._limited(stale, bounded_limit)
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
                self._entries.move_to_end(thread_id)
                while len(self._entries) > self._max_sessions:
                    self._entries.popitem(last=False)
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
                    and self._clock() - cached.fetched_at <= self._status_fresh_seconds
                ):
                    self._statuses.move_to_end(thread_id)
                    return thread_id, copy.deepcopy(cached.status)
            try:
                snapshot = self._read_status_snapshot(thread_id)
            except (CodexAdapterError, AttributeError):
                return thread_id, self._cached_status(thread_id)
            status = self._snapshot_status(snapshot)
            with self._lock:
                self._statuses[thread_id] = _StatusEntry(status, self._clock())
                self._statuses.move_to_end(thread_id)
                while len(self._statuses) > self._max_status_entries:
                    self._statuses.popitem(last=False)
            return thread_id, copy.deepcopy(status)

        futures = {
            self._executor.submit(read_status, thread_id): thread_id
            for thread_id in unique_ids
        }
        results: dict[str, dict | None] = {}
        deadline = self._clock() + self._wait_seconds
        for future, thread_id in futures.items():
            try:
                _, status = future.result(timeout=max(0.0, deadline - self._clock()))
            except FutureTimeoutError:
                future.cancel()
                # Discarding a usable cached status here made the PWA report
                # "unknown" for sessions it had just measured, which the drawer
                # renders as "stopped". An older-but-real answer is strictly
                # more useful than no answer.
                status = self._cached_status(thread_id)
            results[thread_id] = status
        return results

    def _read_status_snapshot(self, thread_id: str) -> object:
        """Read a session's lifecycle without loading its transcript.

        ``read_status`` costs roughly 30ms where a full ``thread/read`` measured
        1.6-6.9s on long sessions. Falling back keeps compatibility with
        adapters that only expose the older method.
        """

        reader = getattr(self._adapter, "read_status", None)
        if callable(reader):
            return reader(thread_id)
        return self._adapter.read_thread(thread_id)

    def _cached_status(self, thread_id: str) -> dict | None:
        with self._lock:
            cached = self._statuses.get(thread_id)
            if cached is None:
                return None
            self._statuses.move_to_end(thread_id)
            return copy.deepcopy(cached.status)

    def stats(self) -> dict:
        with self._lock:
            return {
                "sessions": len(self._entries),
                "maxSessions": self._max_sessions,
                "cachedTurns": sum(
                    len(entry.detail.get("turns") or ())
                    for entry in self._entries.values()
                ),
                "inflightReads": len(self._inflight),
                "statusEntries": len(self._statuses),
                "maxStatusEntries": self._max_status_entries,
                "revision": self._revision,
                "generation": self._generation,
            }

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
        result = dict(detail)
        turns = detail.get("turns")
        result["turns"] = [
            copy.deepcopy(turn) for turn in turns[-turn_limit:]
        ] if isinstance(turns, list) else []
        return result

    @staticmethod
    def _has_error_evidence(turn: object) -> bool:
        """Whether a turn carries something a recovery rule could match.

        Real snapshots expose ``has_error_evidence``; lightweight test doubles
        and older adapters only carry the raw fields, so fall back to them
        instead of silently dropping the error state.
        """

        explicit = getattr(turn, "has_error_evidence", None)
        if explicit is not None:
            return bool(explicit)
        return bool(
            getattr(turn, "error_message", "")
            or getattr(turn, "error_kind", "")
            or getattr(turn, "diagnostic_text", "")
            or getattr(turn, "http_status", None) is not None
        )

    @staticmethod
    def _snapshot_status(snapshot: object) -> dict:
        latest = getattr(snapshot, "latest_turn", None)
        evidence = getattr(snapshot, "evidence_turn", None)
        in_flight = bool(latest is not None and getattr(latest, "in_flight", False))
        evidence = evidence if evidence is not None else latest
        return {
            "statusKnown": True,
            "threadStatus": getattr(snapshot, "thread_status", ""),
            "activeFlags": list(getattr(snapshot, "active_flags", ())),
            # Report a still-running turn as in progress. The App Server labels
            # such a turn ``interrupted`` while it is being written, and the PWA
            # would otherwise show "已中断" for a session that never stopped.
            "latestTurnStatus": (
                "inProgress"
                if in_flight
                else (getattr(latest, "status", "") if latest else "")
            ),
            "latestTurnInFlight": in_flight,
            "latestTurnHasError": bool(
                evidence and SessionProjection._has_error_evidence(evidence)
            ),
            "latestTurnHttpStatus": (
                getattr(evidence, "http_status", None) if evidence else None
            ),
        }
