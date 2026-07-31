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
        poll_seconds: float = 1.0,
        now_provider: Callable[[], str] = now_utc,
        local_date_provider: Callable[[], str] = local_date,
    ) -> None:
        self._service = service
        self._store = store
        self._poll_seconds = poll_seconds
        self._now_provider = now_provider
        self._local_date_provider = local_date_provider
        self._stop_event = threading.Event()
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
            self._worker = threading.Thread(
                target=self._run,
                name="watchdog-scheduler",
                daemon=False,
            )
            self.worker_count_created += 1
            self._worker.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._service.run_due(self._now_provider())
            except Exception as error:
                self.last_error = error
            prune_date = self._local_date_provider()
            if self._store is not None and prune_date != self._last_prune_date:
                self._last_prune_date = prune_date
                try:
                    self._store.prune_records(self._now_provider())
                except Exception as error:
                    self.last_error = error
            self._stop_event.wait(self._poll_seconds)
