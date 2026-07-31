from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from watchdog.codex_adapter import (
    CodexAdapterError,
    CodexAppServerAdapter,
    CodexProtocolError,
    DefiniteSendFailure,
    RpcTransport,
    StdioJsonRpcClient,
    UncertainSendFailure,
    _codex_executable,
)


THREAD_ID = "019fa619-0c95-76c3-a151-9289b7510e09"


class FakeTransport(RpcTransport):
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return self.responses[method]

    def close(self) -> None:
        return


class CodexAdapterTests(unittest.TestCase):
    def test_read_thread_uses_latest_turn_error_as_source_of_truth(self) -> None:
        transport = FakeTransport(
            {
                "thread/read": {
                    "thread": {
                        "id": THREAD_ID,
                        "name": "woxsheet",
                        "status": {"type": "idle"},
                        "turns": [
                            {"id": "turn-old", "status": "completed", "items": []},
                            {
                                "id": "turn-1",
                                "status": "failed",
                                "error": {
                                    "message": "upstream unavailable",
                                    "codexErrorInfo": {
                                        "httpConnectionFailed": {
                                            "httpStatusCode": 503
                                        }
                                    },
                                },
                                "items": [],
                            },
                        ],
                    }
                }
            }
        )

        snapshot = CodexAppServerAdapter(transport).read_thread(THREAD_ID)

        self.assertEqual(snapshot.thread_id, THREAD_ID)
        self.assertEqual(snapshot.name, "woxsheet")
        self.assertEqual(snapshot.thread_status, "idle")
        self.assertIsNotNone(snapshot.latest_turn)
        self.assertEqual(snapshot.latest_turn.id, "turn-1")
        self.assertEqual(snapshot.latest_turn.status, "failed")
        self.assertEqual(snapshot.latest_turn.error_message, "upstream unavailable")
        self.assertEqual(snapshot.latest_turn.error_kind, "httpConnectionFailed")
        self.assertEqual(snapshot.latest_turn.http_status, 503)
        self.assertEqual(
            transport.calls,
            [
                (
                    "thread/read",
                    {"threadId": THREAD_ID, "includeTurns": True},
                )
            ],
        )

    def test_codex_error_requires_exactly_one_documented_error_key(self) -> None:
        errors = (
            None,
            {"codexErrorInfo": "httpConnectionFailed"},
            {"codexErrorInfo": {}},
            {
                "codexErrorInfo": {
                    "httpConnectionFailed": {"httpStatusCode": 503},
                    "responseTooLarge": {},
                }
            },
        )

        for error in errors:
            with self.subTest(error=error):
                transport = FakeTransport(
                    {
                        "thread/read": {
                            "thread": {
                                "id": THREAD_ID,
                                "name": "test",
                                "status": {"type": "idle"},
                                "turns": [
                                    {
                                        "id": "turn-1",
                                        "status": "failed",
                                        "error": error,
                                    }
                                ],
                            }
                        }
                    }
                )
                turn = CodexAppServerAdapter(transport).read_thread(
                    THREAD_ID
                ).latest_turn
                self.assertIsNotNone(turn)
                self.assertEqual(turn.error_kind, "")
                self.assertIsNone(turn.http_status)

    def test_start_turn_sends_one_text_input_without_thread_overrides(self) -> None:
        transport = FakeTransport(
            {
                "turn/start": {
                    "turn": {
                        "id": "turn-new",
                        "status": "inProgress",
                        "items": [],
                    }
                }
            }
        )

        turn_id = CodexAppServerAdapter(transport).start_turn(
            "thread-1", "继续当前开发任务"
        )

        self.assertEqual(turn_id, "turn-new")
        self.assertEqual(
            transport.calls,
            [
                (
                    "turn/start",
                    {
                        "threadId": "thread-1",
                        "input": [
                            {"type": "text", "text": "继续当前开发任务"}
                        ],
                    },
                )
            ],
        )
        params = transport.calls[0][1]
        self.assertNotIn("approvalPolicy", params)
        self.assertNotIn("approvalsReviewer", params)

    def test_start_turn_timeout_or_eof_is_an_uncertain_send_failure(self) -> None:
        class FailingTransport:
            def __init__(self, error: BaseException) -> None:
                self.error = error

            def request(self, method: str, params: dict) -> dict:
                raise self.error

            def close(self) -> None:
                return

        for error in (TimeoutError("late"), EOFError("closed")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(UncertainSendFailure):
                    CodexAppServerAdapter(FailingTransport(error)).start_turn(
                        THREAD_ID, "continue"
                    )

    def test_malformed_turn_start_response_is_a_protocol_error(self) -> None:
        adapter = CodexAppServerAdapter(FakeTransport({"turn/start": {}}))

        with self.assertRaises(CodexProtocolError) as raised:
            adapter.start_turn(THREAD_ID, "continue")

        self.assertIsInstance(raised.exception, CodexAdapterError)
        self.assertNotIsInstance(raised.exception, DefiniteSendFailure)
        self.assertNotIsInstance(raised.exception, UncertainSendFailure)

    def test_read_thread_rejects_malformed_protocol_responses(self) -> None:
        malformed = (
            {},
            {"thread": {"id": "other", "turns": []}},
            {"thread": {"id": THREAD_ID, "turns": {}}},
        )

        for response in malformed:
            with self.subTest(response=response):
                adapter = CodexAppServerAdapter(
                    FakeTransport({"thread/read": response})
                )
                with self.assertRaises(CodexProtocolError):
                    adapter.read_thread(THREAD_ID)

    def test_list_threads_normalizes_thread_summaries(self) -> None:
        transport = FakeTransport(
            {
                "thread/list": {
                    "data": [
                        {
                            "id": THREAD_ID,
                            "name": "watchdog-integration-test",
                            "status": {"type": "idle"},
                        }
                    ]
                }
            }
        )

        sessions = CodexAppServerAdapter(transport).list_threads(limit=5)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].thread_id, THREAD_ID)
        self.assertEqual(sessions[0].name, "watchdog-integration-test")
        self.assertIsNone(sessions[0].latest_turn)
        self.assertEqual(transport.calls, [("thread/list", {"limit": 5})])


class QueueStdout:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def push(self, message: dict) -> None:
        self.lines.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self.lines.put(None)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.lines.get(timeout=2)
        if line is None:
            raise StopIteration
        return line


class RecordingStdin:
    def __init__(self, stdout: QueueStdout) -> None:
        self.stdout = stdout
        self.messages: list[dict] = []
        self.closed = False

    def write(self, text: str) -> int:
        message = json.loads(text)
        self.messages.append(message)
        if message.get("method") == "initialize":
            self.stdout.push({"id": message["id"], "result": {}})
        return len(text)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = QueueStdout()
        self.stdin = RecordingStdin(self.stdout)
        self.stderr = None
        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.stdout.close()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("codex", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.close()


class StdioJsonRpcClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process = FakeProcess()
        self.attention: list[tuple[str, str]] = []
        self.command_patch = patch(
            "watchdog.codex_adapter._codex_executable", return_value="codex"
        )
        self.command_patch.start()
        self.popen_patch = patch(
            "watchdog.codex_adapter.subprocess.Popen", return_value=self.process
        )
        self.popen = self.popen_patch.start()
        self.client = StdioJsonRpcClient(
            on_attention=lambda thread_id, method: self.attention.append(
                (thread_id, method)
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.popen_patch.stop()
        self.command_patch.stop()

    def wait_for_messages(self, count: int) -> None:
        deadline = time.monotonic() + 1
        while len(self.process.stdin.messages) < count and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreaterEqual(len(self.process.stdin.messages), count)

    def test_starts_child_with_exact_initialize_handshake(self) -> None:
        expected_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.popen.assert_called_once_with(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=expected_flags,
        )
        self.assertEqual(
            self.process.stdin.messages[:2],
            [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "localhost-project-console",
                            "title": "Local Project Console",
                            "version": "1.0",
                        },
                        "capabilities": None,
                    },
                },
                {"method": "initialized"},
            ],
        )

    def test_command_approval_request_is_declined_exactly(self) -> None:
        self.process.stdout.push(
            {
                "id": 91,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": THREAD_ID},
            }
        )

        self.wait_for_messages(3)

        self.assertEqual(
            self.process.stdin.messages[2],
            {"id": 91, "result": {"decision": "decline"}},
        )
        self.assertEqual(
            self.attention,
            [(THREAD_ID, "item/commandExecution/requestApproval")],
        )

    def test_other_server_requests_never_approve_work(self) -> None:
        requests = [
            {
                "id": 92,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": THREAD_ID},
            },
            {
                "id": 93,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": THREAD_ID,
                    "questions": [{"id": "q1"}, {"id": "q2"}],
                },
            },
            {
                "id": 94,
                "method": "unknown/request",
                "params": {"threadId": THREAD_ID},
            },
        ]
        for message in requests:
            self.process.stdout.push(message)

        self.wait_for_messages(5)

        responses = self.process.stdin.messages[2:]
        self.assertEqual(
            responses,
            [
                {"id": 92, "result": {"decision": "decline"}},
                {
                    "id": 93,
                    "result": {
                        "answers": {
                            "q1": {"answers": []},
                            "q2": {"answers": []},
                        }
                    },
                },
                {
                    "id": 94,
                    "error": {"code": -32601, "message": "Method not found"},
                },
            ],
        )
        self.assertNotIn("accept", repr(responses))
        self.assertNotIn("acceptForSession", repr(responses))

    def test_request_times_out_and_close_terminates_only_owned_child(self) -> None:
        with self.assertRaises(UncertainSendFailure):
            self.client.request("thread/read", {"threadId": THREAD_ID}, timeout=0.02)

        self.client.close()

        self.assertTrue(self.process.stdin.closed)
        self.assertTrue(self.process.terminated)
        self.assertFalse(self.process.killed)

    def test_explicit_json_rpc_error_is_a_definite_failure(self) -> None:
        errors: list[BaseException] = []

        def request() -> None:
            try:
                self.client.request("turn/start", {"threadId": THREAD_ID})
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=request)
        worker.start()
        self.wait_for_messages(3)
        request_id = self.process.stdin.messages[2]["id"]
        self.process.stdout.push(
            {
                "id": request_id,
                "error": {"code": -32602, "message": "invalid params"},
            }
        )
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DefiniteSendFailure)


class WindowsCommandResolutionTests(unittest.TestCase):
    def test_windows_prefers_native_binary_behind_npm_shim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            npm_root = Path(temp_dir)
            command_shim = npm_root / "codex.cmd"
            command_shim.touch()
            native = (
                npm_root
                / "node_modules"
                / "@openai"
                / "codex"
                / "node_modules"
                / "@openai"
                / "codex-win32-x64"
                / "vendor"
                / "x86_64-pc-windows-msvc"
                / "bin"
                / "codex.exe"
            )
            native.parent.mkdir(parents=True)
            native.touch()
            resolved = {
                "codex.cmd": str(command_shim),
                "codex.exe": r"C:\apps\codex.exe",
            }
            with (
                patch("watchdog.codex_adapter.os.name", "nt"),
                patch(
                    "watchdog.codex_adapter.shutil.which",
                    side_effect=lambda name: resolved.get(name),
                ),
            ):
                command = _codex_executable()

        self.assertEqual(command, str(native))


if __name__ == "__main__":
    unittest.main()
