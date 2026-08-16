import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import app


class ConsoleRuntimeTests(unittest.TestCase):
    class NoopInstanceLock:
        def acquire(self) -> None:
            return

        def release(self) -> None:
            return

    def test_console_instance_lock_rejects_a_second_process(self) -> None:
        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "console.lock"
            first = app.ConsoleInstanceLock(lock_path)
            second = app.ConsoleInstanceLock(lock_path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    second.acquire()
            finally:
                first.release()

            second.acquire()
            second.release()

    def test_codex_startup_failure_keeps_console_runtime_available(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.object(app, "StdioJsonRpcClient", side_effect=OSError("missing")):
                runtime = app.create_console_runtime(Path(directory))

            response = runtime.api.dispatch(
                "GET", "/api/watchdog/status", {}, None
            )
            self.assertEqual(response.status, 200)
            self.assertFalse(response.body["codexConnected"])
            runtime.adapter.close()

    def test_project_console_never_waits_for_auxiliary_runtime(self) -> None:
        events: list[str] = []

        class Supervisor:
            def start(self) -> None:
                events.append("auxiliary-start")

            def stop(self) -> None:
                events.append("auxiliary-stop")

        class Server:
            def serve_forever(self) -> None:
                events.append("http-serve")

            def server_close(self) -> None:
                events.append("server-close")

        with (
            patch.object(
                app,
                "create_console_runtime",
                side_effect=AssertionError("core must not initialize auxiliary runtime"),
            ),
            patch.object(
                app,
                "AuxiliaryRuntimeSupervisor",
                return_value=Supervisor(),
            ),
            patch.object(app, "ThreadingHTTPServer", return_value=Server()),
        ):
            app.run_console(Path("."), instance_lock=self.NoopInstanceLock())

        self.assertEqual(
            events,
            ["auxiliary-start", "http-serve", "auxiliary-stop", "server-close"],
        )

    def test_early_server_failure_does_not_start_background_resources(self) -> None:
        events: list[str] = []

        class Supervisor:
            def start(self) -> None:
                events.append("auxiliary-start")

            def stop(self) -> None:
                events.append("auxiliary-stop")

        class Server:
            def serve_forever(self) -> None:
                events.append("serve")
                raise RuntimeError("server failed")

            def server_close(self) -> None:
                events.append("server-close")

        with (
            patch.object(
                app,
                "AuxiliaryRuntimeSupervisor",
                return_value=Supervisor(),
            ),
            patch.object(app, "ThreadingHTTPServer", return_value=Server()),
            self.assertRaises(RuntimeError),
        ):
            app.run_console(Path("."), instance_lock=self.NoopInstanceLock())

        self.assertEqual(
            events,
            ["auxiliary-start", "serve", "auxiliary-stop", "server-close"],
        )

    def test_auxiliary_runtime_owns_optional_tunnel_lifecycle(self) -> None:
        events: list[str] = []

        class Scheduler:
            def start(self) -> None:
                events.append("scheduler-start")

            def stop(self) -> None:
                events.append("scheduler-stop")

        class Tunnel:
            def start(self) -> None:
                events.append("tunnel-start")

            def stop(self) -> None:
                events.append("tunnel-stop")

        class Adapter:
            def close(self) -> None:
                events.append("adapter-close")

        class Server:
            def serve_forever(self) -> None:
                events.append("serve")
                raise RuntimeError("server failed")

            def server_close(self) -> None:
                events.append("server-close")

        runtime = SimpleNamespace(
            api=object(),
            scheduler=Scheduler(),
            adapter=Adapter(),
            tunnel=Tunnel(),
        )
        with (
            patch.object(app, "create_console_runtime", return_value=runtime),
            patch.object(app, "ThreadingHTTPServer", return_value=Server()),
            self.assertRaises(RuntimeError),
        ):
            app.run_auxiliary_runtime(
                Path("."), instance_lock=self.NoopInstanceLock()
            )

        self.assertEqual(
            events,
            [
                "tunnel-start",
                "scheduler-start",
                "serve",
                "scheduler-stop",
                "tunnel-stop",
                "adapter-close",
                "server-close",
            ],
        )


if __name__ == "__main__":
    unittest.main()
