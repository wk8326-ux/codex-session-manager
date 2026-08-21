"""Register this repository as an ordinary Local Project Console project."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = "http://127.0.0.1:8765"
PROJECT_NAME = "Codex 会话管理"


def request(method: str, path: str, payload: dict | None = None):
    body = (
        None
        if payload is None
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    headers = {"Content-Type": "application/json; charset=utf-8"} if body else {}
    with urlopen(
        Request(f"{CONSOLE}{path}", data=body, headers=headers, method=method),
        timeout=3,
    ) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else None


def project_payload() -> dict:
    return {
        "mode": "local",
        "name": PROJECT_NAME,
        "path": str(ROOT),
        "startCommand": "start-session-manager.bat",
        "stopCommand": "stop-session-manager.bat",
        "port": "8767",
        "url": "http://127.0.0.1:8767/",
        "note": "Codex API 监控、异常续跑与 PWA 远程控制",
    }


def main() -> int:
    try:
        projects = request("GET", "/api/projects")
        existing = next(
            (
                item
                for item in projects
                if (
                    item.get("name") == PROJECT_NAME
                    or (bool(item.get("path")) and Path(item["path"]).resolve() == ROOT)
                )
            ),
            None,
        )
        payload = project_payload()
        if existing:
            request("PUT", f"/api/projects/{existing['id']}", payload)
            print("Codex 会话管理已更新到项目控制台。")
        else:
            request("POST", "/api/projects", payload)
            print("Codex 会话管理已添加到项目控制台。")
        return 0
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        print(f"无法连接项目控制台：{error}", file=sys.stderr)
        print("请先启动 Local Project Console，再运行此脚本。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
