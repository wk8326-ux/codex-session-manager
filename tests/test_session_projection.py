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


if __name__ == "__main__":
    unittest.main()
