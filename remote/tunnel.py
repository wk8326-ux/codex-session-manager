from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FrpTunnelManager:
    """Own the optional frpc child process for the console lifetime."""

    def __init__(
        self,
        executable: Path,
        config_path: Path,
        log_path: Path,
    ) -> None:
        self.executable = Path(executable)
        self.config_path = Path(config_path)
        self.log_path = Path(log_path)
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log_file = None
        self._started_at = ""
        self._last_error = ""

    @classmethod
    def from_base_path(cls, base_path: Path) -> "FrpTunnelManager":
        return cls.from_runtime_path(Path(base_path) / ".runtime")

    @classmethod
    def from_runtime_path(cls, runtime_path: Path) -> "FrpTunnelManager":
        runtime_dir = Path(runtime_path) / "frp"
        default_executable = runtime_dir / (
            "frpc-lpc.exe" if os.name == "nt" else "frpc-lpc"
        )
        return cls(
            Path(os.environ.get("LPC_FRPC_EXECUTABLE", default_executable)),
            Path(os.environ.get("LPC_FRPC_CONFIG", runtime_dir / "frpc.toml")),
            Path(os.environ.get("LPC_FRPC_LOG", runtime_dir / "frpc.log")),
        )

    def configured(self) -> bool:
        return self.executable.is_file() and self.config_path.is_file()

    def running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True
            if not self.configured():
                return False

            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self.log_path.open("a", encoding="utf-8")
            try:
                self._process = subprocess.Popen(
                    [str(self.executable), "-c", str(self.config_path)],
                    cwd=str(self.executable.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=0,
                )
            except OSError as error:
                self._last_error = str(error)
                self._close_log()
                return False
            self._started_at = _utc_now()
            self._last_error = ""
            return True

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            self._close_log()

    def status(self) -> dict:
        with self._lock:
            configured = self.configured()
            running = self._process is not None and self._process.poll() is None
            exit_code = (
                self._process.poll()
                if self._process is not None and not running
                else None
            )
            if running:
                state = "running"
            elif not configured:
                state = "not-configured"
            elif self._last_error or (exit_code is not None and exit_code != 0):
                state = "failed"
            else:
                state = "stopped"
            return {
                "provider": "frp",
                "configured": configured,
                "running": running,
                "state": state,
                "pid": self._process.pid if running and self._process else None,
                "startedAt": self._started_at if running else "",
                "detail": self._last_error,
            }

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
