from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from watchdog.codex_adapter import CodexAppServerAdapter, StdioJsonRpcClient


TEST_THREAD_NAME = "watchdog-integration-test"


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


def main() -> int:
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
                "refusing turn/start: thread name must be exactly "
                f"{TEST_THREAD_NAME!r}"
            )
        turn_id = adapter.start_turn(args.thread_id, args.prompt)
        print(json.dumps({"turnId": turn_id}, ensure_ascii=False))
        return 0
    finally:
        adapter.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"probe failed: {error}", file=sys.stderr)
        raise SystemExit(1)
