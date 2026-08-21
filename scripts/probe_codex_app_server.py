from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from watchdog.codex_adapter import CodexAppServerAdapter


TEST_THREAD_NAME = "watchdog-integration-test"
SEND_CONFIRMATION_TIMEOUT_SECONDS = 90.0


def uuid_value(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("thread ID must be a UUID") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Codex App Server protocol probe by default."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--list", action="store_true", help="list local threads")
    target.add_argument("--thread-id", type=uuid_value, help="read one local thread")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--allow-send", action="store_true")
    parser.add_argument("--prompt")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.allow_send and args.thread_id is None:
        parser.error("--allow-send requires --thread-id")
    if args.allow_send and not args.prompt:
        parser.error("--allow-send requires a non-empty --prompt")
    if args.prompt and not args.allow_send:
        parser.error("--prompt requires --allow-send")
    return args


def wait_for_turn_completion(
    adapter: CodexAppServerAdapter, thread_id: str, turn_id: str
) -> str:
    deadline = time.monotonic() + SEND_CONFIRMATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        snapshot = adapter.read_thread(thread_id)
        turn = snapshot.latest_turn
        if turn is not None and turn.id == turn_id:
            if turn.status == "completed":
                return turn.status
            if turn.status == "failed":
                detail = turn.error_message or "unknown App Server error"
                raise RuntimeError(f"test turn failed: {detail}")
            if turn.status in {"interrupted", "cancelled"}:
                raise RuntimeError(f"test turn ended with status {turn.status!r}")
        time.sleep(0.25)
    raise TimeoutError("test turn did not complete before the send gate timeout")


def main() -> int:
    from watchdog.codex_adapter import CodexAppServerAdapter, StdioJsonRpcClient

    args = parse_args()
    client = StdioJsonRpcClient(
        on_attention=lambda thread_id, method: print(
            f"attention required: thread={thread_id!r} method={method!r}",
            file=sys.stderr,
        )
    )
    adapter = CodexAppServerAdapter(client)
    try:
        if args.list:
            for snapshot in adapter.list_threads(limit=args.limit):
                print(json.dumps(asdict(snapshot), ensure_ascii=False))
            return 0

        snapshot = adapter.read_thread(args.thread_id)
        print(json.dumps(asdict(snapshot), ensure_ascii=False))
        if not args.allow_send:
            return 0
        if snapshot.name != TEST_THREAD_NAME:
            raise RuntimeError(
                f"refusing turn/start: thread name must be exactly {TEST_THREAD_NAME!r}"
            )
        turn_id = adapter.start_turn(args.thread_id, args.prompt)
        print(json.dumps({"turnId": turn_id}, ensure_ascii=False))
        status = wait_for_turn_completion(adapter, args.thread_id, turn_id)
        print(
            json.dumps(
                {"confirmedTurnId": turn_id, "status": status},
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"probe failed: {error}", file=sys.stderr)
        raise SystemExit(1)
