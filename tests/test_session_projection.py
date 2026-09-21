from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

from remote.projection import SessionProjection


class Adapter:
    def __init__(self) -> None:
        self.calls = 0
        self.release = threading.Event()

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        self.calls += 1
        self.release.wait(1)
        return {
            "threadId": thread_id,
            "status": "idle",
            "activeFlags": [],
            "latestTurnStatus": "completed",
            "latestTurnError": "",
            "latestTurnHttpStatus": None,
            "turns": [{"id": str(index)} for index in range(10)],
        }

    def read_thread(self, thread_id: str):
        self.calls += 1
        self.release.wait(1)
        return SimpleNamespace(
            thread_id=thread_id,
            thread_status="idle",
            active_flags=(),
            latest_turn=SimpleNamespace(
                status="completed", error_message="", http_status=None
            ),
        )


class SessionProjectionTests(unittest.TestCase):
    def test_coalesces_concurrent_reads_and_applies_turn_limit(self) -> None:
        adapter = Adapter()
        projection = SessionProjection(adapter, fresh_seconds=10)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(projection.read("thread", turn_limit=2)))
            for _ in range(3)
        ]
        try:
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 1
            while adapter.calls == 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            adapter.release.set()
            for thread in threads:
                thread.join(timeout=1)

            self.assertEqual(adapter.calls, 1)
            self.assertEqual(len(results), 3)
            self.assertTrue(all(len(result["turns"]) == 2 for result in results))
        finally:
            projection.close()

    def test_invalidation_and_runtime_generation_force_refresh(self) -> None:
        adapter = Adapter()
        adapter.release.set()
        generation = 1
        projection = SessionProjection(
            adapter,
            fresh_seconds=60,
            generation_provider=lambda: generation,
        )
        try:
            projection.read("thread")
            projection.read("thread")
            self.assertEqual(adapter.calls, 1)

            projection.invalidate("thread")
            projection.read("thread")
            self.assertEqual(adapter.calls, 2)

            generation = 2
            projection.read("thread")
            self.assertEqual(adapter.calls, 3)
        finally:
            projection.close()

    def test_list_statuses_reuses_cached_projection(self) -> None:
        adapter = Adapter()
        adapter.release.set()
        projection = SessionProjection(adapter, fresh_seconds=60)
        try:
            first = projection.list_statuses(["one", "two"])
            second = projection.list_statuses(["one", "two"])
            self.assertEqual(adapter.calls, 2)
            self.assertEqual(first["one"]["latestTurnStatus"], "completed")
            self.assertEqual(second, first)
        finally:
            projection.close()

    def test_projection_cache_is_bounded(self) -> None:
        adapter = Adapter()
        adapter.release.set()
        projection = SessionProjection(adapter, fresh_seconds=0, max_sessions=2)
        try:
            for thread_id in ("one", "two", "three"):
                projection.read(thread_id)

            stats = projection.stats()
            self.assertEqual(stats["sessions"], 2)
            self.assertEqual(stats["maxSessions"], 2)
        finally:
            projection.close()

    def test_waiting_reader_falls_back_to_cached_view_instead_of_erroring(
        self,
    ) -> None:
        adapter = Adapter()
        adapter.release.set()
        projection = SessionProjection(adapter, fresh_seconds=0, wait_seconds=0.1)
        try:
            projection.read("thread")
            adapter.release.clear()
            slow = threading.Thread(target=projection.read, args=("thread",))
            slow.start()
            deadline = time.monotonic() + 1
            while adapter.calls < 2 and time.monotonic() < deadline:
                time.sleep(0.005)

            result = projection.read("thread")

            self.assertEqual(result["threadId"], "thread")
            adapter.release.set()
            slow.join(timeout=1)
        finally:
            adapter.release.set()
            projection.close()

    def test_list_statuses_does_not_block_on_a_stalled_adapter(self) -> None:
        class StalledAdapter(Adapter):
            def read_thread(self, thread_id: str):
                self.calls += 1
                self.release.wait(5)
                return super().read_thread(thread_id)

        adapter = StalledAdapter()
        projection = SessionProjection(adapter, fresh_seconds=0, wait_seconds=0.2)
        try:
            started = time.monotonic()
            statuses = projection.list_statuses(["one", "two"])

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(set(statuses), {"one", "two"})
            self.assertIsNone(statuses["one"])
            self.assertIsNone(statuses["two"])
        finally:
            adapter.release.set()
            projection.close()

    def test_status_cache_survives_inside_its_own_freshness_window(self) -> None:
        class StatusAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def read_status(self, thread_id: str):
                self.calls += 1
                return SimpleNamespace(
                    thread_id=thread_id,
                    thread_status="idle",
                    active_flags=(),
                    latest_turn=SimpleNamespace(
                        status="completed",
                        error_message="",
                        http_status=None,
                        in_flight=False,
                    ),
                    evidence_turn=None,
                )

        adapter = StatusAdapter()
        projection = SessionProjection(
            adapter, fresh_seconds=60, status_fresh_seconds=60
        )
        try:
            first = projection.list_statuses(["thread"])
            second = projection.list_statuses(["thread"])

            # A longer status window keeps the cheap lifecycle answer warm while
            # the PWA polls, instead of re-reading every session each heartbeat.
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(first, second)
            self.assertEqual(second["thread"]["latestTurnStatus"], "completed")
        finally:
            projection.close()

    def test_status_timeout_falls_back_to_the_last_known_status(self) -> None:
        class StallingStatusAdapter:
            def __init__(self) -> None:
                self.calls = 0
                self.release = threading.Event()
                self.release.set()

            def read_status(self, thread_id: str):
                self.calls += 1
                self.release.wait(5)
                return SimpleNamespace(
                    thread_id=thread_id,
                    thread_status="idle",
                    active_flags=(),
                    latest_turn=SimpleNamespace(
                        status="inProgress",
                        error_message="",
                        http_status=None,
                        in_flight=True,
                    ),
                    evidence_turn=None,
                )

        adapter = StallingStatusAdapter()
        projection = SessionProjection(
            adapter, status_fresh_seconds=0, wait_seconds=0.2
        )
        try:
            warm = projection.list_statuses(["thread"])
            self.assertEqual(warm["thread"]["latestTurnStatus"], "inProgress")

            adapter.release.clear()
            started = time.monotonic()
            stale = projection.list_statuses(["thread"])

            self.assertLess(time.monotonic() - started, 1.5)
            # Reporting "unknown" for a session that was just measured made the
            # drawer render a live session as stopped, so the cache is served.
            self.assertEqual(stale["thread"]["latestTurnStatus"], "inProgress")
            self.assertTrue(stale["thread"]["latestTurnInFlight"])
        finally:
            adapter.release.set()
            projection.close()

    def test_status_prefers_read_status_over_the_full_thread_read(self) -> None:
        class BothAdapter(Adapter):
            def __init__(self) -> None:
                super().__init__()
                self.status_calls = 0

            def read_status(self, thread_id: str):
                self.status_calls += 1
                return SimpleNamespace(
                    thread_id=thread_id,
                    thread_status="idle",
                    active_flags=(),
                    latest_turn=SimpleNamespace(
                        status="completed",
                        error_message="",
                        http_status=None,
                        in_flight=False,
                    ),
                    evidence_turn=None,
                )

        adapter = BothAdapter()
        adapter.release.set()
        projection = SessionProjection(adapter)
        try:
            projection.list_statuses(["thread"])

            self.assertEqual(adapter.status_calls, 1)
            self.assertEqual(adapter.calls, 0)
        finally:
            projection.close()


if __name__ == "__main__":
    unittest.main()
