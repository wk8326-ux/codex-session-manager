from __future__ import annotations

import os
import signal
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_executable(pid: int) -> Path | None:
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            return Path(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            return None

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _process_matches_executable(pid: int, executable: Path) -> bool:
    actual = _process_executable(pid)
    if actual is None:
        return False
    return os.path.normcase(str(actual.resolve())) == os.path.normcase(
        str(executable.resolve())
    )


def _terminate_process_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


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
        self.pid_path = self.log_path.with_suffix(".pid")
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
            Path(
                os.environ.get("CSM_FRPC_EXECUTABLE")
                or os.environ.get("LPC_FRPC_EXECUTABLE", default_executable)
            ),
            Path(
                os.environ.get("CSM_FRPC_CONFIG")
                or os.environ.get("LPC_FRPC_CONFIG", runtime_dir / "frpc.toml")
            ),
            Path(
                os.environ.get("CSM_FRPC_LOG")
                or os.environ.get("LPC_FRPC_LOG", runtime_dir / "frpc.log")
            ),
        )

    def configured(self) -> bool:
        return self.executable.is_file() and self.config_path.is_file()

    def running(self) -> bool:
        with self._lock:
            return self._running_pid() is not None

    def _persist_pid(self, pid: int) -> None:
        temporary = self.pid_path.with_suffix(".pid.tmp")
        temporary.write_text(str(pid), encoding="ascii")
        temporary.replace(self.pid_path)

    def _persisted_pid(self) -> int | None:
        try:
            pid = int(self.pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            self.pid_path.unlink(missing_ok=True)
            return None
        if _process_matches_executable(pid, self.executable):
            return pid
        self.pid_path.unlink(missing_ok=True)
        return None

    def _running_pid(self) -> int | None:
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        self._process = None
        return self._persisted_pid()

    @staticmethod
    def _stop_child(process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def start(self) -> bool:
        with self._lock:
            if not self.configured():
                return False
            if self._running_pid() is not None:
                return True

            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self.log_path.open("a", encoding="utf-8")
            try:
                self._process = subprocess.Popen(
                    [str(self.executable), "-c", str(self.config_path)],
                    cwd=str(self.executable.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._persist_pid(self._process.pid)
            except OSError as error:
                process = self._process
                self._process = None
                if process is not None and process.poll() is None:
                    self._stop_child(process)
                self.pid_path.unlink(missing_ok=True)
                self._last_error = str(error)
                self._close_log()
                return False
            self._started_at = _utc_now()
            self._last_error = ""
            return True

    def stop(self) -> None:
        with self._lock:
            process = self._process
            persisted_pid = (
                None
                if process is not None and process.poll() is None
                else self._persisted_pid()
            )
            self._process = None
            if process is not None and process.poll() is None:
                self._stop_child(process)
            elif persisted_pid is not None:
                _terminate_process_tree(persisted_pid)
            self.pid_path.unlink(missing_ok=True)
            self._close_log()

    def status(self) -> dict:
        with self._lock:
            configured = self.configured()
            running_pid = self._running_pid()
            running = running_pid is not None
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
                "pid": running_pid,
                "startedAt": self._started_at if running else "",
                "detail": self._last_error,
            }

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
