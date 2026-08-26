from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Protocol


class ScheduledService(Protocol):
    def run_due(self, now: str) -> object:
        raise NotImplementedError


class RecordStore(Protocol):
    def prune_records(self, now: str) -> None:
        raise NotImplementedError

    def next_check_at(self) -> str | None:
        raise NotImplementedError


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date() -> str:
    return datetime.now().astimezone().date().isoformat()


class WatchdogScheduler:
    """Run watchdog cycles serially on one stoppable worker thread."""

    def __init__(
        self,
        service: ScheduledService,
        *,
        store: RecordStore | None = None,
        poll_seconds: float = 60.0,
        now_provider: Callable[[], str] = now_utc,
        local_date_provider: Callable[[], str] = local_date,
    ) -> None:
        self._service = service
        self._store = store
        self._poll_seconds = poll_seconds
        self._now_provider = now_provider
        self._local_date_provider = local_date_provider
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.worker_count_created = 0
        self.last_error: Exception | None = None
        self._last_prune_date: str | None = None

    @property
    def is_running(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._worker = threading.Thread(
                target=self._run,
                name="watchdog-scheduler",
                daemon=False,
            )
            self.worker_count_created += 1
            self._worker.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        self._wake_event.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def wake(self) -> None:
        self._wake_event.set()

    def _wait_seconds(self, now: str) -> float:
        if self._store is None or not hasattr(self._store, "next_check_at"):
            return self._poll_seconds
        next_check = self._store.next_check_at()
        if not next_check:
            return self._poll_seconds
        try:
            current = datetime.fromisoformat(now.replace("Z", "+00:00"))
            due = datetime.fromisoformat(next_check.replace("Z", "+00:00"))
        except ValueError:
            return self._poll_seconds
        return max(0.05, min(self._poll_seconds, (due - current).total_seconds()))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = self._now_provider()
            try:
                self._service.run_due(now)
            except Exception as error:
                self.last_error = error
            prune_date = self._local_date_provider()
            if self._store is not None and prune_date != self._last_prune_date:
                self._last_prune_date = prune_date
                try:
                    self._store.prune_records(self._now_provider())
                except Exception as error:
                    self.last_error = error
            wait_seconds = self._wait_seconds(now)
            self._wake_event.wait(wait_seconds)
            self._wake_event.clear()
