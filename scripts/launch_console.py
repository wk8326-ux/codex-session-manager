"""Open the lightweight project console without loading PowerShell on the hot path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import webbrowser
from http.client import HTTPConnection
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8765
CONSOLE_URL = f"http://{HEALTH_HOST}:{HEALTH_PORT}/"
TASK_NAME = "Local Project Console"


def console_is_ready(timeout: float = 0.15) -> bool:
    connection = HTTPConnection(HEALTH_HOST, HEALTH_PORT, timeout=timeout)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and payload.get("ready") is True
    except (ConnectionError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return False
    finally:
        connection.close()


def start_scheduled_task() -> bool:
    executable = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe")
    result = subprocess.run(
        [executable, "/Run", "/TN", TASK_NAME],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    return result.returncode == 0


def recover_with_management_script() -> bool:
    if os.name != "nt":
        return False
    executable = str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "manage-system-startup.ps1"),
            "-Action",
            "Start",
        ],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    return result.returncode == 0


def wait_until_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if console_is_ready():
            return True
        time.sleep(0.05)
    return console_is_ready()


def main() -> int:
    if not console_is_ready():
        start_scheduled_task()
        if not wait_until_ready(1.25):
            if not recover_with_management_script() or not wait_until_ready(1.0):
                print(
                    "Local Project Console could not be started. "
                    "Run install-system-startup.bat once and try again.",
                    file=sys.stderr,
                )
                return 1
    webbrowser.open(CONSOLE_URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
