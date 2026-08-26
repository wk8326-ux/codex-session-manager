from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time
import tomllib
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


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
        self._last_exit_code: int | None = None

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
        if self._process is not None:
            self._last_exit_code = self._process.poll()
            if self._last_exit_code not in {None, 0}:
                self._last_error = f"frpc exited with code {self._last_exit_code}"
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
            self._last_exit_code = None
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
            if running:
                state = "running"
            elif not configured:
                state = "not-configured"
            elif self._last_error or self._last_exit_code not in {None, 0}:
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
                "exitCode": self._last_exit_code,
            }

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


def _http_probe(url: str, timeout: float) -> dict:
    started = time.monotonic()
    try:
        with urlopen(
            Request(url, headers={"User-Agent": "codex-session-manager-health"}),
            timeout=timeout,
        ) as response:
            response.read(256)
            status = int(response.status)
    except Exception as error:
        return {
            "ok": False,
            "latencyMs": round((time.monotonic() - started) * 1000),
            "detail": f"{type(error).__name__}: {error}",
        }
    return {
        "ok": 200 <= status < 300,
        "httpStatus": status,
        "latencyMs": round((time.monotonic() - started) * 1000),
        "detail": "",
    }


def _relay_target(config_path: Path) -> tuple[str, int] | None:
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        host = str(data.get("serverAddr") or "").strip()
        port = int(data.get("serverPort") or 7000)
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return None
    return (host, port) if host else None


def _tcp_probe(target: tuple[str, int] | None, timeout: float) -> dict:
    if target is None:
        return {"ok": False, "latencyMs": None, "detail": "relay is not configured"}
    started = time.monotonic()
    try:
        with socket.create_connection(target, timeout=timeout):
            pass
    except OSError as error:
        return {
            "ok": False,
            "latencyMs": round((time.monotonic() - started) * 1000),
            "detail": f"{type(error).__name__}: {error}",
        }
    return {
        "ok": True,
        "latencyMs": round((time.monotonic() - started) * 1000),
        "detail": "",
    }


class TunnelSupervisor:
    """Supervise one tunnel adapter using layered end-to-end health."""

    def __init__(
        self,
        adapter: FrpTunnelManager,
        *,
        local_url: str,
        public_url_provider: Callable[[], str],
        check_interval_seconds: float = 20.0,
        failure_threshold: int = 6,
        relay_failure_threshold: int = 3,
        restart_cooldown_seconds: float = 120.0,
        max_restart_cooldown_seconds: float = 900.0,
        probe_timeout_seconds: float = 6.0,
        http_probe: Callable[[str, float], dict] = _http_probe,
        tcp_probe: Callable[[tuple[str, int] | None, float], dict] = _tcp_probe,
    ) -> None:
        self._adapter = adapter
        self._local_url = local_url
        self._public_url_provider = public_url_provider
        self._check_interval = max(1.0, check_interval_seconds)
        self._failure_threshold = max(1, failure_threshold)
        self._relay_failure_threshold = max(1, relay_failure_threshold)
        self._restart_cooldown = max(0.0, restart_cooldown_seconds)
        self._max_restart_cooldown = max(
            self._restart_cooldown, max(0.0, max_restart_cooldown_seconds)
        )
        self._probe_timeout = max(0.1, probe_timeout_seconds)
        self._http_probe = http_probe
        self._tcp_probe = tcp_probe
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._health = self._empty_health()
        self._public_failures = 0
        self._relay_failures = 0
        self._restart_count = 0
        self._last_restart_at = 0.0
        self._next_restart_at = 0.0
        self._restart_backoff_steps = 0
        self._last_restart_reason = ""
        self._recent_restarts: deque[dict] = deque(maxlen=10)

    def start(self) -> bool:
        started = self._adapter.start()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop_event.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name="tunnel-supervisor",
                    daemon=True,
                )
                self._thread.start()
        return started

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._adapter.stop()

    def check_once(self) -> dict:
        base = self._adapter.status()
        local = self._http_probe(self._local_url, self._probe_timeout)
        configured = bool(base.get("configured"))
        relay = self._tcp_probe(
            _relay_target(self._adapter.config_path), self._probe_timeout
        ) if configured else {
            "ok": False,
            "latencyMs": None,
            "detail": "tunnel is not configured",
        }
        public_base = self._public_url_provider().strip().rstrip("/")
        public = (
            self._http_probe(f"{public_base}/api/remote/health", self._probe_timeout)
            if public_base
            else {"ok": False, "latencyMs": None, "detail": "public URL is not configured"}
        )
        if relay.get("ok"):
            self._relay_failures = 0
        else:
            self._relay_failures += 1

        # Public reachability crosses the user's browser/CDN/network path. A
        # timeout there is diagnostic, but restarting a healthy frpc cannot fix
        # it and previously caused self-inflicted mobile disconnects.
        if public.get("ok"):
            self._public_failures = 0
        else:
            self._public_failures += 1

        now = time.monotonic()
        if (
            configured
            and base.get("running")
            and local.get("ok")
            and relay.get("ok")
        ):
            self._restart_backoff_steps = 0
            self._next_restart_at = 0.0

        should_restart = bool(
            configured
            and local.get("ok")
            and (
                not base.get("running")
                or self._relay_failures >= self._relay_failure_threshold
            )
            and now >= self._next_restart_at
        )
        if should_restart:
            reason = (
                "frpc is not running"
                if not base.get("running")
                else "FRP relay is unreachable"
            )
            if base.get("running"):
                self._adapter.stop()
            self._adapter.start()
            now = time.monotonic()
            self._last_restart_at = now
            self._restart_count += 1
            self._last_restart_reason = reason
            self._recent_restarts.append(
                {
                    "at": _utc_now(),
                    "reason": reason,
                    "wasRunning": bool(base.get("running")),
                }
            )
            self._restart_backoff_steps += 1
            delay = min(
                self._max_restart_cooldown,
                self._restart_cooldown * (2 ** (self._restart_backoff_steps - 1)),
            )
            self._next_restart_at = now + delay
            self._public_failures = 0
            base = self._adapter.status()

        checked_at = _utc_now()
        health = {
            "local": {**local, "checkedAt": checked_at},
            "relay": {**relay, "checkedAt": checked_at},
            "public": {**public, "checkedAt": checked_at},
        }
        with self._lock:
            self._health = health
        return self.status()

    def status(self) -> dict:
        base = self._adapter.status()
        with self._lock:
            health = {key: dict(value) for key, value in self._health.items()}
            restart_count = self._restart_count
            failures = self._public_failures
            relay_failures = self._relay_failures
            recent_restarts = list(self._recent_restarts)
            last_restart_reason = self._last_restart_reason
        if not base.get("configured"):
            state = "not-configured"
        elif not base.get("running"):
            state = "failed" if base.get("state") == "failed" else "stopped"
        elif health["local"].get("ok") and health["public"].get("ok"):
            state = "running"
        elif any(item.get("checkedAt") for item in health.values()):
            state = "degraded"
        else:
            state = "starting"
        return {
            **base,
            "state": state,
            "health": health,
            "consecutivePublicFailures": failures,
            "consecutiveRelayFailures": relay_failures,
            "restartCount": restart_count,
            "lastRestartReason": last_restart_reason,
            "recentRestarts": recent_restarts,
        }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.check_once()
            self._stop_event.wait(self._check_interval)

    @staticmethod
    def _empty_health() -> dict:
        return {
            name: {"ok": False, "latencyMs": None, "detail": "not checked", "checkedAt": ""}
            for name in ("local", "relay", "public")
        }
