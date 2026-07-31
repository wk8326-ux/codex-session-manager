import threading
import time
import unittest

from watchdog.scheduler import WatchdogScheduler


class FakeService:
    def __init__(self, errors: list[Exception | None] | None = None) -> None:
        self.errors = list(errors or [])
        self.called = threading.Event()
        self.calls = 0

    def run_due(self, now: str) -> None:
        self.calls += 1
        self.called.set()
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error

    def wait_for_calls(self, count: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.calls >= count:
                return True
            time.sleep(0.01)
        return False


class FakeStore:
    def __init__(self) -> None:
        self.prune_calls: list[str] = []

    def prune_records(self, now: str) -> None:
        self.prune_calls.append(now)


class SchedulerTests(unittest.TestCase):
    def test_start_is_idempotent_and_stop_joins_worker(self) -> None:
        service = FakeService()
        scheduler = WatchdogScheduler(service, poll_seconds=0.01)

        scheduler.start()
        scheduler.start()
        self.assertTrue(service.called.wait(timeout=1))
        scheduler.stop(timeout=1)

        self.assertFalse(scheduler.is_running)
        self.assertEqual(scheduler.worker_count_created, 1)

    def test_service_failure_does_not_kill_scheduler(self) -> None:
        service = FakeService(errors=[RuntimeError("one cycle"), None])
        scheduler = WatchdogScheduler(service, poll_seconds=0.01)
        scheduler.start()
        self.assertTrue(service.wait_for_calls(2, timeout=1))
        scheduler.stop(timeout=1)

        self.assertIsInstance(scheduler.last_error, RuntimeError)
        self.assertFalse(scheduler.is_running)

    def test_records_are_pruned_at_most_once_per_local_day(self) -> None:
        service = FakeService()
        store = FakeStore()
        scheduler = WatchdogScheduler(
            service,
            store=store,
            poll_seconds=0.01,
            now_provider=lambda: "2026-07-31T06:00:00Z",
            local_date_provider=lambda: "2026-07-31",
        )

        scheduler.start()
        self.assertTrue(service.wait_for_calls(3, timeout=1))
        scheduler.stop(timeout=1)

        self.assertEqual(store.prune_calls, ["2026-07-31T06:00:00Z"])


if __name__ == "__main__":
    unittest.main()
