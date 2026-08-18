"""A small local dashboard for starting and monitoring development projects.

Run with: py app.py
Open:     http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.client import HTTPConnection, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from runtime_paths import APP_VERSION, ApplicationPaths


SOURCE_ROOT = Path(__file__).resolve().parent
PATHS = ApplicationPaths.resolve(SOURCE_ROOT)
ROOT = PATHS.resource_root
DATA_ROOT = PATHS.data_root
RUNTIME_ROOT = PATHS.runtime_root
CONFIG_PATH = PATHS.projects_path
LOG_DIR = PATHS.log_root
HOST = "127.0.0.1"
PORT = int(os.environ.get("LPC_ADMIN_PORT", "8765"))
REMOTE_HOST = os.environ.get("LPC_REMOTE_HOST", "0.0.0.0")
REMOTE_PORT = int(os.environ.get("LPC_REMOTE_PORT", "8766"))
AUXILIARY_PORT = int(os.environ.get("LPC_AUXILIARY_PORT", "8767"))
LOCK = threading.RLock()
WEBSITE_CACHE_LOCK = threading.RLock()
WEBSITE_CACHE: dict[str, dict] = {}
WEBSITE_REFRESHING: set[str] = set()
WEBSITE_CACHE_TTL = 15.0
WEBSITE_TIMEOUT = 3.0
WEBSITE_FAILURE_THRESHOLD = 3
WATCHDOG_API: object | None = None
REMOTE_ADMIN_API: object | None = None
REMOTE_HTTP_API: object | None = None
RELAY_SETUP_API: object | None = None
STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")

# Kept patchable for startup-failure tests; the real class is imported lazily.
StdioJsonRpcClient = None


class AuxiliaryRuntimeUnavailable(RuntimeError):
    """The isolated session-monitoring runtime is not accepting requests."""


def auxiliary_worker_command(paths: ApplicationPaths) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--runtime-worker"]
    else:
        command = [
            sys.executable,
            "-u",
            str(SOURCE_ROOT / "app.py"),
            "--runtime-worker",
        ]
    command.extend(
        [
            "--data-dir",
            str(paths.data_root),
            "--runtime-dir",
            str(paths.runtime_root),
            "--log-dir",
            str(paths.log_root),
            "--mode",
            paths.mode,
        ]
    )
    return command


class ConsoleInstanceLock:
    """Hold one cross-process lock for a console-owned process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            handle.close()
            raise RuntimeError("Local Project Console is already running.") from error

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def auxiliary_runtime_ready() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.05)
        return connection.connect_ex((HOST, AUXILIARY_PORT)) == 0


def proxy_auxiliary_request(
    method: str,
    path: str,
    payload: object,
) -> tuple[int, object]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    connection = HTTPConnection(HOST, AUXILIARY_PORT, timeout=120)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
    except (ConnectionError, OSError, RemoteDisconnected, TimeoutError) as error:
        raise AuxiliaryRuntimeUnavailable(
            "Session monitoring runtime is unavailable"
        ) from error
    finally:
        connection.close()
    if not raw:
        return response.status, None
    try:
        return response.status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuxiliaryRuntimeUnavailable(
            "Session monitoring runtime returned an invalid response"
        ) from error


class AuxiliaryRuntimeSupervisor:
    """Keep heavyweight Codex, monitoring, remote, and FRP work off the core."""

    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        startup_delay: float = 0.6,
        restart_delay: float = 3.0,
    ) -> None:
        self.paths = paths
        self.startup_delay = startup_delay
        self.restart_delay = restart_delay
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="auxiliary-runtime-supervisor",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        if self._stop_event.wait(self.startup_delay):
            return
        log_directory = self.paths.system_log_root
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / "auxiliary-runtime.log"
        previous_log_path = log_directory / "auxiliary-runtime.previous.log"
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            previous_log_path.unlink(missing_ok=True)
            log_path.replace(previous_log_path)
        command = auxiliary_worker_command(self.paths)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        while not self._stop_event.is_set():
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                log.write(f"[{now()}] Starting auxiliary runtime.\n")
                process = subprocess.Popen(
                    command,
                    cwd=str(self.paths.resource_root),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                with self._process_lock:
                    self._process = process
                while process.poll() is None and not self._stop_event.wait(0.25):
                    pass
                if self._stop_event.is_set() and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                log.write(
                    f"[{now()}] Auxiliary runtime exited with code "
                    f"{process.returncode}.\n"
                )
            with self._process_lock:
                self._process = None
            if self._stop_event.wait(self.restart_delay):
                return

    def stop(self) -> None:
        self._stop_event.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        return None

DEFAULT_PROJECTS: list[dict] = []


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_projects() -> list[dict]:
    if not CONFIG_PATH.exists():
        save_projects(DEFAULT_PROJECTS)
        return DEFAULT_PROJECTS
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, list) else DEFAULT_PROJECTS
    except (OSError, json.JSONDecodeError):
        return DEFAULT_PROJECTS


def save_projects(projects: list[dict]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = CONFIG_PATH.with_suffix(f"{CONFIG_PATH.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(projects, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(CONFIG_PATH)


STATELESS_COMMANDS = {"--migrate-from", "--version"}
PROJECTS = (
    [] if any(argument in STATELESS_COMMANDS for argument in sys.argv) else load_projects()
)


def reorder_projects(projects: list[dict], ordered_ids: object) -> bool:
    """Apply a complete project order without accepting missing or duplicate IDs."""
    if not isinstance(ordered_ids, list) or not all(
        isinstance(project_id, str) for project_id in ordered_ids
    ):
        return False
    existing_ids = [str(project.get("id", "")) for project in projects]
    if len(ordered_ids) != len(existing_ids) or set(ordered_ids) != set(existing_ids):
        return False
    by_id = {str(project.get("id", "")): project for project in projects}
    projects[:] = [by_id[project_id] for project_id in ordered_ids]
    return True


def project_url_is_valid(value: object, *, external: bool) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return not external
    parsed = urlparse(raw)
    allowed_schemes = {"http", "https"}
    return (
        parsed.scheme.lower() in allowed_schemes
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def website_port_is_open(value: object) -> tuple[bool, int | None]:
    try:
        parsed = urlparse(str(value or "").strip())
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if not parsed.hostname:
            return False, None
        with socket.create_connection((parsed.hostname, port), timeout=1.5):
            return True, port
    except (OSError, TypeError, ValueError):
        return False, None


def probe_website(value: object) -> dict:
    raw = str(value or "").strip()
    checked_at = now()
    if not project_url_is_valid(raw, external=True):
        return {
            "online": False,
            "status": None,
            "detail": "网址无效",
            "checkedAt": checked_at,
        }

    request = Request(
        raw,
        headers={
            "User-Agent": "LocalProjectConsole/1.0",
            "Range": "bytes=0-0",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        method="GET",
    )
    # This request sends no credentials and measures reachability only. Browser
    # navigation still performs its normal certificate validation.
    handlers: list[object] = [NoRedirectHandler()]
    if urlparse(raw).scheme.lower() == "https":
        handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
    opener = build_opener(*handlers)
    try:
        with opener.open(request, timeout=WEBSITE_TIMEOUT) as response:
            status = int(response.getcode())
    except HTTPError as error:
        status = int(error.code)
    except (URLError, TimeoutError, OSError):
        port_active, port = website_port_is_open(raw)
        if port_active:
            return {
                "online": True,
                "status": None,
                "detail": f"端口 {port} 可达 · HTTP 复检中",
                "checkedAt": checked_at,
            }
        return {
            "online": False,
            "status": None,
            "detail": "连接失败",
            "checkedAt": checked_at,
        }

    online = 200 <= status < 500 and status not in {404, 410}
    if online and status in {401, 403}:
        detail = f"HTTP {status} · 需要登录"
    else:
        detail = f"HTTP {status}"
    return {
        "online": online,
        "status": status,
        "detail": detail,
        "checkedAt": checked_at,
    }


def cached_website_health(value: object) -> dict:
    url = str(value or "").strip()
    current = time.monotonic()
    with WEBSITE_CACHE_LOCK:
        cached = WEBSITE_CACHE.get(url)
        if cached and current - cached["refreshedAt"] < WEBSITE_CACHE_TTL:
            return dict(cached["health"])

    health = probe_website(url)
    return store_website_health(url, health, refreshed_at=current)


def store_website_health(
    url: str, health: dict, *, refreshed_at: float | None = None
) -> dict:
    current = time.monotonic() if refreshed_at is None else refreshed_at
    with WEBSITE_CACHE_LOCK:
        previous = WEBSITE_CACHE.get(url)
        if health.get("online"):
            displayed = dict(health)
            last_success = dict(health)
            failures = 0
        else:
            failures = int(previous.get("failures", 0)) + 1 if previous else 1
            last_success = previous.get("lastSuccess") if previous else None
            if last_success and failures < WEBSITE_FAILURE_THRESHOLD:
                displayed = dict(last_success)
                displayed["checkedAt"] = health.get("checkedAt", "")
                displayed["detail"] = (
                    f"{last_success.get('detail', '上次在线')} · "
                    f"复检中 {failures}/{WEBSITE_FAILURE_THRESHOLD}"
                )
            else:
                displayed = dict(health)
        WEBSITE_CACHE[url] = {
            "refreshedAt": current,
            "health": displayed,
            "lastSuccess": last_success,
            "failures": failures,
        }
    return dict(displayed)


def background_website_health(value: object) -> dict:
    """Return cached reachability immediately and refresh stale URLs in the background."""
    url = str(value or "").strip()
    current = time.monotonic()
    should_refresh = False
    with WEBSITE_CACHE_LOCK:
        cached = WEBSITE_CACHE.get(url)
        if cached and current - cached["refreshedAt"] < WEBSITE_CACHE_TTL:
            return dict(cached["health"])
        if url not in WEBSITE_REFRESHING:
            WEBSITE_REFRESHING.add(url)
            should_refresh = True
        displayed = dict(cached["health"]) if cached else {
            "online": False,
            "pending": True,
            "status": None,
            "detail": "检测中",
            "checkedAt": "",
        }

    if should_refresh:
        def refresh() -> None:
            try:
                store_website_health(url, probe_website(url))
            finally:
                with WEBSITE_CACHE_LOCK:
                    WEBSITE_REFRESHING.discard(url)

        threading.Thread(
            target=refresh,
            name=f"website-probe-{abs(hash(url))}",
            daemon=True,
        ).start()
    return displayed


def port_is_open(value: object) -> bool:
    try:
        port = int(str(value))
        if not 1 <= port <= 65535:
            return False
    except (TypeError, ValueError):
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.1)
        return connection.connect_ex((HOST, port)) == 0


def pid_is_running(value: object) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid < 1:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def running_pids(values: list[object]) -> set[int]:
    requested: set[int] = set()
    for value in values:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            requested.add(pid)
    if not requested:
        return set()
    return {pid for pid in requested if pid_is_running(pid)}


def state_for(
    project: dict,
    *,
    website_health: dict | None = None,
    pid_active: bool | None = None,
    port_active: bool | None = None,
) -> dict:
    external = project.get("mode") == "external"
    external_ready = project_url_is_valid(project.get("url"), external=True)
    pid_active = pid_is_running(project.get("pid")) if pid_active is None else pid_active
    port_active = port_is_open(project.get("port")) if port_active is None else port_active
    configured = bool(project.get("startCommand", "").strip())
    health = website_health or {}
    if external and not external_ready:
        state = "needs-config"
        label = "待配置"
    elif external and health.get("pending"):
        state = "checking"
        label = "检测中"
    elif external:
        health = website_health or cached_website_health(project.get("url"))
        if health.get("online"):
            state = "online"
            label = "在线"
        else:
            state = "offline"
            label = "离线"
    elif pid_active or port_active:
        state = "running"
        label = "运行中"
    elif not configured:
        state = "needs-config"
        label = "待配置"
    else:
        state = "stopped"
        label = "已关闭"
    return {
        **project,
        "mode": "external" if external else "local",
        "pathExists": external or Path(project.get("path", "")).is_dir(),
        "pidActive": pid_active,
        "portActive": port_active,
        "websiteActive": bool(health.get("online")) if external_ready else False,
        "websiteStatus": health.get("status") if external_ready else None,
        "websiteDetail": health.get("detail", "") if external_ready else "",
        "websiteCheckedAt": health.get("checkedAt", "") if external_ready else "",
        "websitePending": bool(health.get("pending")) if external_ready else False,
        "state": state,
        "stateLabel": label,
    }


def states_for(projects: list[dict]) -> list[dict]:
    health_by_id = {
        project["id"]: background_website_health(project.get("url"))
        for project in projects
        if project.get("mode") == "external"
        and project_url_is_valid(project.get("url"), external=True)
    }
    active_pids = running_pids([project.get("pid") for project in projects])
    local_projects = [project for project in projects if project.get("mode") != "external"]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(local_projects)))) as executor:
        port_by_id = {
            project["id"]: is_open
            for project, is_open in zip(
                local_projects,
                executor.map(port_is_open, [project.get("port") for project in local_projects]),
            )
        }
    return [
        state_for(
            project,
            website_health=health_by_id.get(project.get("id")),
            port_active=port_by_id.get(project.get("id"), False),
            pid_active=(
                int(project["pid"]) in active_pids
                if str(project.get("pid") or "").isdigit()
                else False
            ),
        )
        for project in projects
    ]


def project_summary(projects: list[dict]) -> dict:
    local = [project for project in projects if project.get("mode") != "external"]
    counts = {
        "all": len(projects),
        "running": 0,
        "stopped": 0,
        "needs-config": 0,
        "external": 0,
    }
    for project in projects:
        if project.get("mode") == "external":
            counts["external"] += 1
        state = project.get("state")
        if state in counts and state != "external":
            counts[state] += 1
    return {
        "runningCount": sum(project.get("state") == "running" for project in local),
        "localCount": len(local),
        "counts": counts,
    }


def find_project(project_id: str) -> dict | None:
    return next((project for project in PROJECTS if project["id"] == project_id), None)


def write_log(project: dict, message: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    filename = f"{project['id']}.log"
    with (LOG_DIR / filename).open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{now()}] {message}\n")


def start_project(project: dict) -> tuple[bool, str]:
    if project.get("mode") == "external":
        return False, "外部网页不由本机控制台启动。"
    command = project.get("startCommand", "").strip()
    path = Path(project.get("path", ""))
    if not command:
        return False, "请先填写启动命令。"
    if not path.is_dir():
        return False, "项目目录不存在，请检查工作目录。"
    if state_for(project)["state"] == "running":
        return False, "项目已经在运行。"

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{project['id']}.log"
    with log_path.open("a", encoding="utf-8") as output:
        output.write(f"\n[{now()}] START: {command}\n")
        process = subprocess.Popen(
            command,
            cwd=str(path),
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
    project["pid"] = process.pid
    project["startedAt"] = now()
    write_log(project, f"Launcher PID {process.pid}")
    return True, "已发出启动命令。"


def stop_project(project: dict) -> tuple[bool, str]:
    if project.get("mode") == "external":
        return False, "外部网页不由本机控制台关闭。"
    command = project.get("stopCommand", "").strip()
    path = Path(project.get("path", ""))
    if command and path.is_dir():
        result = subprocess.run(
            command,
            cwd=str(path),
            shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        write_log(project, f"STOP command (exit {result.returncode}): {command}")
    pid = project.get("pid")
    if pid_is_running(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        write_log(project, f"Terminated process tree rooted at PID {pid}")
    project["pid"] = None
    project["startedAt"] = ""
    return True, "已发出关闭命令。"


@dataclass
class ConsoleRuntime:
    api: object
    scheduler: object
    adapter: object
    remote_admin_api: object
    remote_http_api: object
    tunnel: object
    relay_setup_api: object


class UnavailableCodexAdapter:
    def __init__(self, error_type: type[Exception] = RuntimeError) -> None:
        self._error_type = error_type

    def _unavailable(self) -> None:
        raise self._error_type("Codex App Server is unavailable")

    def read_thread(self, thread_id: str):
        self._unavailable()

    def list_threads(self, limit: int = 5):
        self._unavailable()

    def start_turn(self, thread_id: str, prompt: str) -> str:
        self._unavailable()

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        self._unavailable()

    def send_message(
        self, thread_id: str, prompt: str, image_url: str | None = None
    ) -> dict:
        self._unavailable()

    def close(self) -> None:
        return


def _default_remote_base_url() -> str:
    addresses = []
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    host = next(
        (address for address in addresses if not address.startswith("127.")),
        "127.0.0.1",
    )
    return f"http://{host}:{REMOTE_PORT}"


def _remote_projects() -> list[dict]:
    projects = [dict(project) for project in load_projects()]
    return states_for(projects)


def create_console_runtime(
    base_path: Path,
    *,
    runtime_path: Path | None = None,
    resource_path: Path | None = None,
) -> ConsoleRuntime:
    from remote.application import RemoteApplication
    from remote.approvals import RemoteApprovalBroker
    from remote.events import RemoteEventHub
    from remote.native_capture import FlameshotRegionCapture
    from remote.router import AdminRemoteApi, RemoteHttpApi
    from remote.setup import RelaySetupApi, RelaySetupService
    from remote.store import RemoteStore
    from remote.tunnel import FrpTunnelManager
    from watchdog.application import WatchdogApplication
    from watchdog.channels import probe_channel
    from watchdog.codex_adapter import (
        CodexAdapterError,
        CodexAppServerAdapter,
        StdioJsonRpcClient as DefaultStdioJsonRpcClient,
    )
    from watchdog.http_api import WatchdogHttpApi
    from watchdog.scheduler import WatchdogScheduler
    from watchdog.secrets import DpapiSecretStore
    from watchdog.service import WatchdogService
    from watchdog.store import WatchdogStore

    base_path = Path(base_path).resolve()
    runtime_path = (
        Path(runtime_path).resolve()
        if runtime_path is not None
        else base_path / ".runtime"
    )
    resource_path = (
        Path(resource_path).resolve()
        if resource_path is not None
        else base_path
    )
    store = WatchdogStore(base_path / "watchdog.db")
    store.initialize()
    secrets = DpapiSecretStore(
        entropy=b"localhost-project-console/watchdog/v1"
    )
    remote_store = RemoteStore(base_path / "watchdog.db")
    remote_store.initialize()
    event_hub = RemoteEventHub(
        lambda: {
            session["threadId"] for session in remote_store.list_synced_sessions()
        }
    )
    approval_broker = RemoteApprovalBroker(
        lambda: {
            session["threadId"] for session in remote_store.list_synced_sessions()
        },
        publish_event=event_hub.publish,
        record_audit=remote_store.record_approval_audit,
    )
    codex_connected = True
    try:
        client_factory = StdioJsonRpcClient or DefaultStdioJsonRpcClient
        client = client_factory(
            approval_policy=lambda thread_id, _turn_id: bool(
                store.get_settings()["resumeActionsEnabled"]
                and store.unattended_approvals_enabled(thread_id)
            ),
            approval_broker=approval_broker,
        )
        adapter: object = CodexAppServerAdapter(client)
    except Exception:
        adapter = UnavailableCodexAdapter(CodexAdapterError)
        codex_connected = False
    monitor_service = WatchdogService(
        store, secrets, probe_channel, adapter
    )
    tunnel = FrpTunnelManager.from_runtime_path(runtime_path)
    if codex_connected:
        def handle_event(method: str, params: dict) -> None:
            try:
                monitor_service.handle_app_server_event(method, params)
            finally:
                event_hub.publish(method, params)

        client.set_event_handler(handle_event)
    scheduler = WatchdogScheduler(monitor_service, store=store)
    application = WatchdogApplication(
        store,
        secrets,
        probe_channel,
        adapter,
        monitor_service,
        scheduler,
        codex_connected=codex_connected,
    )
    remote_application = RemoteApplication(
        remote_store,
        adapter,
        event_hub,
        _remote_projects,
        default_base_url=_default_remote_base_url(),
        codex_connected=codex_connected,
        approval_broker=approval_broker,
        tunnel_status_provider=tunnel.status,
        tunnel_start_provider=tunnel.start,
    )
    relay_setup_api = RelaySetupApi(
        RelaySetupService(
            resource_path,
            runtime_path=runtime_path,
            tunnel_status_provider=tunnel.status,
            tunnel_start_provider=tunnel.start,
        )
    )
    native_capture = FlameshotRegionCapture()
    return ConsoleRuntime(
        WatchdogHttpApi(application),
        scheduler,
        adapter,
        AdminRemoteApi(remote_application, native_capture=native_capture.capture),
        RemoteHttpApi(remote_application),
        tunnel,
        relay_setup_api,
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True

    def respond_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def respond_from_auxiliary(
        self,
        method: str,
        path: str,
        payload: object,
    ) -> None:
        try:
            status, body = proxy_auxiliary_request(method, path, payload)
        except AuxiliaryRuntimeUnavailable:
            self.respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "会话监控与远程会话仍在启动，请稍后重试。"},
            )
            return
        if status == HTTPStatus.NO_CONTENT:
            self.respond_empty(status)
        else:
            self.respond_json(status, body)

    def respond_file(
        self, path: Path, content_type: str, *, cache: str | None = None
    ) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if cache:
            self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_api_response(self, response: ApiResponse) -> None:
        if response.status == HTTPStatus.NO_CONTENT:
            self.send_response(response.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.respond_json(response.status, response.body)

    def respond_remote_response(self, response: RemoteResponse) -> None:
        if response.status == HTTPStatus.NO_CONTENT:
            self.send_response(response.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.respond_json(response.status, response.body)

    def dispatch_remote_admin(self, method: str, path: str, payload: object) -> bool:
        if path != "/api/remote" and not path.startswith("/api/remote/"):
            return False
        if REMOTE_ADMIN_API is None:
            self.respond_from_auxiliary(method, path, payload)
            return True
        response = REMOTE_ADMIN_API.dispatch(method, path, payload)
        if response is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
        else:
            self.respond_remote_response(response)
        return True

    def dispatch_relay_setup(self, method: str, path: str, payload: object) -> bool:
        if path != "/api/relay-setup" and not path.startswith("/api/relay-setup/"):
            return False
        if RELAY_SETUP_API is None:
            self.respond_from_auxiliary(method, path, payload)
            return True
        response = RELAY_SETUP_API.dispatch(method, path, payload)
        if response is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
        else:
            self.respond_json(response.status, response.body)
        return True

    def dispatch_watchdog(
        self, method: str, parsed: object, payload: object
    ) -> bool:
        path = parsed.path
        if path != "/api/watchdog" and not path.startswith("/api/watchdog/"):
            return False
        if WATCHDOG_API is None:
            path_with_query = path
            if parsed.query:
                path_with_query = f"{path}?{parsed.query}"
            self.respond_from_auxiliary(method, path_with_query, payload)
            return True
        response = WATCHDOG_API.dispatch(
            method,
            path,
            parse_qs(parsed.query, keep_blank_values=True),
            payload,
        )
        if response is None:
            return False
        self.respond_api_response(response)
        return True

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.respond_json(
                HTTPStatus.OK,
                {
                    "service": "local-project-console",
                    "version": APP_VERSION,
                    "ready": True,
                    "auxiliaryReady": auxiliary_runtime_ready(),
                    "mode": PATHS.mode,
                    "pid": os.getpid(),
                    "startedAt": STARTED_AT,
                    "ports": {
                        "admin": PORT,
                        "remote": REMOTE_PORT,
                        "auxiliary": AUXILIARY_PORT,
                    },
                },
            )
            return
        if self.dispatch_watchdog("GET", parsed, None):
            return
        if self.dispatch_remote_admin("GET", self.path, None):
            return
        if self.dispatch_relay_setup("GET", parsed.path, None):
            return
        if parsed.path == "/api/projects":
            with LOCK:
                projects = [dict(project) for project in PROJECTS]
            self.respond_json(HTTPStatus.OK, states_for(projects))
            return
        if parsed.path == "/api/shell/project-summary":
            with LOCK:
                projects = [dict(project) for project in PROJECTS]
            self.respond_json(
                HTTPStatus.OK,
                project_summary(states_for(projects)),
            )
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/log"):
            project_id = parsed.path.split("/")[3]
            log_path = LOG_DIR / f"{project_id}.log"
            content = log_path.read_text(encoding="utf-8", errors="replace")[-12000:] if log_path.exists() else "暂无日志。"
            self.respond_json(HTTPStatus.OK, {"content": content})
            return
        if parsed.path in {"/watchdog", "/watchdog/"}:
            self.respond_file(ROOT / "watchdog.html", "text/html; charset=utf-8")
            return
        if parsed.path in {"/remote", "/remote/"}:
            self.respond_file(
                ROOT / "remote.html", "text/html; charset=utf-8", cache="no-cache"
            )
            return
        if parsed.path == "/remote.css":
            self.respond_file(
                ROOT / "remote.css", "text/css; charset=utf-8", cache="no-cache"
            )
            return
        if parsed.path == "/remote.js":
            self.respond_file(
                ROOT / "remote.js", "text/javascript; charset=utf-8", cache="no-cache"
            )
            return
        if parsed.path in {"/remote-setup", "/remote-setup/"}:
            self.respond_file(
                ROOT / "remote-setup.html", "text/html; charset=utf-8", cache="no-cache"
            )
            return
        if parsed.path == "/remote-setup.css":
            self.respond_file(
                ROOT / "remote-setup.css", "text/css; charset=utf-8", cache="no-cache"
            )
            return
        if parsed.path == "/remote-setup.js":
            self.respond_file(
                ROOT / "remote-setup.js", "text/javascript; charset=utf-8", cache="no-cache"
            )
            return
        if parsed.path == "/assets/console-sidebar.css":
            self.respond_file(
                ROOT / "assets" / "console-sidebar.css", "text/css; charset=utf-8"
            )
            return
        if parsed.path == "/assets/console-sidebar.js":
            self.respond_file(
                ROOT / "assets" / "console-sidebar.js", "text/javascript; charset=utf-8"
            )
            return
        if parsed.path == "/assets/vendor/qrcode.min.js":
            self.respond_file(ROOT / "assets" / "vendor" / "qrcode.min.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/assets/vendor/jsQR.js":
            self.respond_file(ROOT / "assets" / "vendor" / "jsQR.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/manifest.webmanifest":
            self.respond_file(ROOT / "manifest.webmanifest", "application/manifest+json")
            return
        if parsed.path == "/service-worker.js":
            self.respond_file(ROOT / "service-worker.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/assets/project-console-icon.png":
            self.respond_file(ROOT / "assets" / "project-console-icon.png", "image/png")
            return
        if parsed.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
        except (json.JSONDecodeError, ValueError):
            self.respond_json(HTTPStatus.BAD_REQUEST, {"message": "请求格式错误。"})
            return
        parsed = urlparse(self.path)
        if self.dispatch_watchdog("POST", parsed, payload):
            return
        if self.dispatch_remote_admin("POST", parsed.path, payload):
            return
        if self.dispatch_relay_setup("POST", parsed.path, payload):
            return
        with LOCK:
            if parsed.path == "/api/projects/reorder":
                if not reorder_projects(PROJECTS, payload.get("ids")):
                    self.respond_json(
                        HTTPStatus.BAD_REQUEST,
                        {"message": "项目顺序无效，请刷新页面后重试。"},
                    )
                    return
                save_projects(PROJECTS)
                self.respond_json(HTTPStatus.OK, {"ids": [project["id"] for project in PROJECTS]})
                return
            if parsed.path == "/api/projects":
                project = {
                    "id": uuid.uuid4().hex,
                    "mode": "external" if payload.get("mode") == "external" else "local",
                    "name": str(payload.get("name", "新项目")).strip() or "新项目",
                    "path": str(payload.get("path", "")).strip(),
                    "startCommand": str(payload.get("startCommand", "")).strip(),
                    "stopCommand": str(payload.get("stopCommand", "")).strip(),
                    "port": str(payload.get("port", "")).strip(),
                    "url": str(payload.get("url", "")).strip(),
                    "note": str(payload.get("note", "")).strip(),
                    "pid": None,
                    "startedAt": "",
                }
                external = project["mode"] == "external"
                if not project_url_is_valid(project["url"], external=external):
                    self.respond_json(
                        HTTPStatus.BAD_REQUEST,
                        {"message": "外部网页必须使用有效的 HTTP 或 HTTPS 地址。" if external else "访问链接必须使用 HTTP 或 HTTPS 地址。"},
                    )
                    return
                if external:
                    project.update({"path": "", "startCommand": "", "stopCommand": "", "port": ""})
                PROJECTS.append(project)
                save_projects(PROJECTS)
                self.respond_json(HTTPStatus.CREATED, state_for(project))
                return
            project_id = parsed.path.split("/")[3] if len(parsed.path.split("/")) > 3 else ""
            project = find_project(project_id)
            if not project:
                self.respond_json(HTTPStatus.NOT_FOUND, {"message": "项目不存在。"})
                return
            if parsed.path.endswith("/start"):
                ok, message = start_project(project)
            elif parsed.path.endswith("/stop"):
                ok, message = stop_project(project)
            else:
                self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
                return
            save_projects(PROJECTS)
            self.respond_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"message": message, "project": state_for(project)})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/watchdog" or parsed.path.startswith(
            "/api/watchdog/"
        ):
            try:
                payload = self.read_json()
            except (json.JSONDecodeError, ValueError):
                self.respond_json(
                    HTTPStatus.BAD_REQUEST, {"message": "Invalid JSON request."}
                )
                return
            self.dispatch_watchdog("PUT", parsed, payload)
            return
        project_id = urlparse(self.path).path.split("/")[-1]
        with LOCK:
            project = find_project(project_id)
            if not project:
                self.respond_json(HTTPStatus.NOT_FOUND, {"message": "项目不存在。"})
                return
            try:
                payload = self.read_json()
            except (json.JSONDecodeError, ValueError):
                self.respond_json(HTTPStatus.BAD_REQUEST, {"message": "请求格式错误。"})
                return
            candidate = dict(project)
            for field in (
                "name",
                "mode",
                "path",
                "startCommand",
                "stopCommand",
                "port",
                "url",
                "note",
            ):
                if field in payload:
                    candidate[field] = str(payload[field]).strip()
            candidate["mode"] = "external" if candidate.get("mode") == "external" else "local"
            external = candidate["mode"] == "external"
            if not project_url_is_valid(candidate.get("url"), external=external):
                self.respond_json(
                    HTTPStatus.BAD_REQUEST,
                    {"message": "外部网页必须使用有效的 HTTP 或 HTTPS 地址。" if external else "访问链接必须使用 HTTP 或 HTTPS 地址。"},
                )
                return
            if external:
                candidate.update({"path": "", "startCommand": "", "stopCommand": "", "port": ""})
            project.update(candidate)
            save_projects(PROJECTS)
            self.respond_json(HTTPStatus.OK, state_for(project))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if self.dispatch_watchdog("DELETE", parsed, None):
            return
        if self.dispatch_remote_admin("DELETE", parsed.path, None):
            return
        project_id = urlparse(self.path).path.split("/")[-1]
        with LOCK:
            project = find_project(project_id)
            if not project:
                self.respond_json(HTTPStatus.NOT_FOUND, {"message": "项目不存在。"})
                return
            if state_for(project)["state"] == "running":
                self.respond_json(HTTPStatus.CONFLICT, {"message": "请先关闭运行中的项目。"})
                return
            PROJECTS.remove(project)
            save_projects(PROJECTS)
            self.respond_json(HTTPStatus.NO_CONTENT, {})


class AuxiliaryHandler(Handler):
    """Internal HTTP boundary for the heavyweight background runtime."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auxiliary-health":
            self.respond_json(HTTPStatus.OK, {"ready": True})
            return
        if self.dispatch_watchdog("GET", parsed, None):
            return
        if self.dispatch_remote_admin("GET", self.path, None):
            return
        if self.dispatch_relay_setup("GET", parsed.path, None):
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.respond_json(HTTPStatus.BAD_REQUEST, {"message": "请求格式错误。"})
            return
        parsed = urlparse(self.path)
        if self.dispatch_watchdog("POST", parsed, payload):
            return
        if self.dispatch_remote_admin("POST", self.path, payload):
            return
        if self.dispatch_relay_setup("POST", parsed.path, payload):
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})

    def do_PUT(self) -> None:
        try:
            payload = self.read_json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.respond_json(HTTPStatus.BAD_REQUEST, {"message": "请求格式错误。"})
            return
        parsed = urlparse(self.path)
        if self.dispatch_watchdog("PUT", parsed, payload):
            return
        if self.dispatch_remote_admin("PUT", self.path, payload):
            return
        if self.dispatch_relay_setup("PUT", parsed.path, payload):
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if self.dispatch_watchdog("DELETE", parsed, None):
            return
        if self.dispatch_remote_admin("DELETE", self.path, None):
            return
        if self.dispatch_relay_setup("DELETE", parsed.path, None):
            return
        self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})


class RemoteHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True

    def _headers(self, content_type: str, length: int, *, cache: str = "no-store") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def respond_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def respond_file(self, path: Path, content_type: str, *, cache: str = "public, max-age=3600") -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self._headers(content_type, len(body), cache=cache)
        if path.name == "service-worker.js":
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_100_000:
            raise ValueError("request too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def dispatch_api(self, method: str, payload: object = None) -> bool:
        if not self.path.startswith("/api/remote/"):
            return False
        if REMOTE_HTTP_API is None:
            self.respond_json(HTTPStatus.SERVICE_UNAVAILABLE, {"message": "Remote workspace is unavailable."})
            return True
        headers = {key: value for key, value in self.headers.items()}
        response = REMOTE_HTTP_API.dispatch(method, self.path, headers, payload)
        if response is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
        elif response.status == HTTPStatus.NO_CONTENT:
            self.send_response(response.status)
            self._headers("application/json; charset=utf-8", 0)
            self.end_headers()
        else:
            self.respond_json(response.status, response.body)
        return True

    def do_GET(self) -> None:
        if self.dispatch_api("GET"):
            return
        path = urlparse(self.path).path
        static = {
            "/": ("remote.html", "text/html; charset=utf-8", "no-cache"),
            "/remote": ("remote.html", "text/html; charset=utf-8", "no-cache"),
            "/remote/": ("remote.html", "text/html; charset=utf-8", "no-cache"),
            "/pair": ("remote.html", "text/html; charset=utf-8", "no-cache"),
            "/remote.css": ("remote.css", "text/css; charset=utf-8", "no-cache"),
            "/remote.js": ("remote.js", "text/javascript; charset=utf-8", "no-cache"),
            "/assets/console-sidebar.css": ("assets/console-sidebar.css", "text/css; charset=utf-8", "public, max-age=3600"),
            "/assets/console-sidebar.js": ("assets/console-sidebar.js", "text/javascript; charset=utf-8", "public, max-age=3600"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", "no-cache"),
            "/service-worker.js": ("service-worker.js", "text/javascript; charset=utf-8", "no-cache"),
            "/assets/project-console-icon.png": ("assets/project-console-icon.png", "image/png", "public, max-age=86400"),
            "/assets/vendor/qrcode.min.js": ("assets/vendor/qrcode.min.js", "text/javascript; charset=utf-8", "public, max-age=86400"),
            "/assets/vendor/jsQR.js": ("assets/vendor/jsQR.js", "text/javascript; charset=utf-8", "public, max-age=86400"),
        }
        asset = static.get(path)
        if asset is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
            return
        self.respond_file(ROOT / asset[0], asset[1], cache=asset[2])

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.respond_json(HTTPStatus.BAD_REQUEST, {"message": "请求格式错误。"})
            return
        if not self.dispatch_api("POST", payload):
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})


def _paths_for_base(base_path: Path | None) -> ApplicationPaths:
    if base_path is None or Path(base_path).resolve() == PATHS.data_root:
        return PATHS
    resolved = Path(base_path).resolve()
    return ApplicationPaths(
        resource_root=resolved,
        data_root=resolved,
        runtime_root=resolved / ".runtime",
        log_root=resolved / "logs",
        mode="source",
    )


def run_auxiliary_runtime(
    base_path: Path | None = None,
    instance_lock: ConsoleInstanceLock | None = None,
) -> None:
    global WATCHDOG_API, REMOTE_ADMIN_API, REMOTE_HTTP_API, RELAY_SETUP_API
    paths = _paths_for_base(base_path)
    paths.ensure_writable_directories()
    lock = instance_lock or ConsoleInstanceLock(
        paths.system_runtime_root / "auxiliary.lock"
    )
    lock.acquire()
    runtime = None
    auxiliary_server = None
    remote_server = None
    remote_thread = None
    tunnel = None
    scheduler_started = False
    try:
        runtime = create_console_runtime(
            paths.data_root,
            runtime_path=paths.runtime_root,
            resource_path=paths.resource_root,
        )
        WATCHDOG_API = runtime.api
        REMOTE_ADMIN_API = getattr(runtime, "remote_admin_api", None)
        REMOTE_HTTP_API = getattr(runtime, "remote_http_api", None)
        RELAY_SETUP_API = getattr(runtime, "relay_setup_api", None)
        auxiliary_server = ThreadingHTTPServer(
            (HOST, AUXILIARY_PORT), AuxiliaryHandler
        )
        if REMOTE_HTTP_API is not None:
            remote_server = ThreadingHTTPServer(
                (REMOTE_HOST, REMOTE_PORT), RemoteHandler
            )
            remote_thread = threading.Thread(
                target=remote_server.serve_forever,
                name="remote-workspace-http",
                daemon=True,
            )
            remote_thread.start()
        tunnel = getattr(runtime, "tunnel", None)
        if tunnel is not None:
            tunnel.start()
        runtime.scheduler.start()
        scheduler_started = True
        print(
            f"Session monitoring runtime is ready at "
            f"http://{HOST}:{AUXILIARY_PORT}"
        )
        if remote_server is not None:
            print(f"Remote workspace is listening on {REMOTE_HOST}:{REMOTE_PORT}")
        auxiliary_server.serve_forever()
    finally:
        if scheduler_started and runtime is not None:
            runtime.scheduler.stop()
        if tunnel is not None:
            tunnel.stop()
        if runtime is not None:
            runtime.adapter.close()
        if remote_server is not None:
            if remote_thread is not None:
                remote_server.shutdown()
            remote_server.server_close()
        if remote_thread is not None:
            remote_thread.join(timeout=2)
        if auxiliary_server is not None:
            auxiliary_server.server_close()
        WATCHDOG_API = None
        REMOTE_ADMIN_API = None
        REMOTE_HTTP_API = None
        RELAY_SETUP_API = None
        lock.release()


def run_console(
    base_path: Path | None = None,
    instance_lock: ConsoleInstanceLock | None = None,
    auxiliary_supervisor: object | None = None,
) -> None:
    paths = _paths_for_base(base_path)
    paths.ensure_writable_directories()
    lock = instance_lock or ConsoleInstanceLock(
        paths.system_runtime_root / "console.lock"
    )
    lock.acquire()
    server = None
    supervisor = auxiliary_supervisor
    try:
        paths.log_root.mkdir(parents=True, exist_ok=True)
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        if supervisor is None:
            supervisor = AuxiliaryRuntimeSupervisor(paths)
        print(f"Local Project Console is available at http://{HOST}:{PORT}")
        supervisor.start()
        server.serve_forever()
    finally:
        try:
            if supervisor is not None:
                supervisor.stop()
        finally:
            if server is not None:
                server.server_close()
            lock.release()


def run_service(base_path: Path | None = None) -> None:
    paths = _paths_for_base(base_path)
    paths.ensure_writable_directories()
    log_directory = paths.system_log_root
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / "console-service.log"
    previous_log_path = log_directory / "console-service.previous.log"
    if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
        previous_log_path.unlink(missing_ok=True)
        log_path.replace(previous_log_path)

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        print(f"[{now()}] Starting Local Project Console with {sys.executable}")
        try:
            run_console(paths.data_root)
        except BaseException:
            import traceback

            traceback.print_exc()
            raise
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


def main() -> None:
    arguments = sys.argv[1:]
    if "--version" in arguments:
        print(APP_VERSION)
        return
    if "--migrate-from" in arguments:
        from runtime_migration import migrate_legacy_data

        source_index = arguments.index("--migrate-from") + 1
        if source_index >= len(arguments):
            raise SystemExit("--migrate-from requires a source directory")
        result = migrate_legacy_data(Path(arguments[source_index]), PATHS)
        print(
            json.dumps(
                {
                    "migrated": result.migrated,
                    "source": str(result.source),
                    "destination": str(result.destination),
                    "copied": list(result.copied),
                },
                ensure_ascii=False,
            )
        )
        return
    if "--runtime-worker" in arguments:
        run_auxiliary_runtime(PATHS.data_root)
        return
    if "--service" in arguments:
        run_service(PATHS.data_root)
        return
    run_console()


if __name__ == "__main__":
    main()
