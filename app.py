"""A small local dashboard for starting and monitoring development projects.

Run with: py app.py
Open:     http://127.0.0.1:8765
"""

from __future__ import annotations

import csv
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from remote.application import RemoteApplication
from remote.events import RemoteEventHub
from remote.router import AdminRemoteApi, RemoteHttpApi, RemoteResponse
from remote.store import RemoteStore
from remote.tunnel import FrpTunnelManager

from watchdog.application import WatchdogApplication
from watchdog.channels import probe_channel
from watchdog.codex_adapter import (
    CodexAdapterError,
    CodexAppServerAdapter,
    StdioJsonRpcClient,
)
from watchdog.http_api import ApiResponse, WatchdogHttpApi
from watchdog.scheduler import WatchdogScheduler
from watchdog.secrets import DpapiSecretStore
from watchdog.service import WatchdogService
from watchdog.store import WatchdogStore


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "projects.json"
LOG_DIR = ROOT / "logs"
HOST = "127.0.0.1"
PORT = int(os.environ.get("LPC_ADMIN_PORT", "8765"))
REMOTE_HOST = os.environ.get("LPC_REMOTE_HOST", "0.0.0.0")
REMOTE_PORT = int(os.environ.get("LPC_REMOTE_PORT", "8766"))
LOCK = threading.RLock()
WEBSITE_CACHE_LOCK = threading.RLock()
WEBSITE_CACHE: dict[str, dict] = {}
WEBSITE_REFRESHING: set[str] = set()
WEBSITE_CACHE_TTL = 15.0
WEBSITE_TIMEOUT = 3.0
WEBSITE_FAILURE_THRESHOLD = 3
WATCHDOG_API: WatchdogHttpApi | None = None
REMOTE_ADMIN_API: AdminRemoteApi | None = None
REMOTE_HTTP_API: RemoteHttpApi | None = None


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
    CONFIG_PATH.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding="utf-8")


PROJECTS = load_projects()


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
        connection.settimeout(0.3)
        return connection.connect_ex((HOST, port)) == 0


def pid_is_running(value: object) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid < 1:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    return str(pid) in result.stdout


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
    if len(requested) == 1:
        pid = next(iter(requested))
        return {pid} if pid_is_running(pid) else set()
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    active: set[int] = set()
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1].replace(",", ""))
        except ValueError:
            continue
        if pid in requested:
            active.add(pid)
    return active


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
    api: WatchdogHttpApi
    scheduler: WatchdogScheduler
    adapter: object
    remote_admin_api: AdminRemoteApi
    remote_http_api: RemoteHttpApi
    tunnel: FrpTunnelManager


class UnavailableCodexAdapter:
    def read_thread(self, thread_id: str):
        raise CodexAdapterError("Codex App Server is unavailable")

    def list_threads(self, limit: int = 5):
        raise CodexAdapterError("Codex App Server is unavailable")

    def start_turn(self, thread_id: str, prompt: str) -> str:
        raise CodexAdapterError("Codex App Server is unavailable")

    def read_thread_detail(self, thread_id: str, turn_limit: int = 30) -> dict:
        raise CodexAdapterError("Codex App Server is unavailable")

    def send_message(self, thread_id: str, prompt: str) -> dict:
        raise CodexAdapterError("Codex App Server is unavailable")

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
    with LOCK:
        projects = [dict(project) for project in PROJECTS]
    return states_for(projects)


def create_console_runtime(base_path: Path) -> ConsoleRuntime:
    store = WatchdogStore(base_path / "watchdog.db")
    store.initialize()
    secrets = DpapiSecretStore(
        entropy=b"localhost-project-console/watchdog/v1"
    )
    codex_connected = True
    try:
        client = StdioJsonRpcClient(
            approval_policy=lambda thread_id, _turn_id: bool(
                store.get_settings()["resumeActionsEnabled"]
                and store.unattended_approvals_enabled(thread_id)
            )
        )
        adapter: object = CodexAppServerAdapter(client)
    except Exception:
        adapter = UnavailableCodexAdapter()
        codex_connected = False
    monitor_service = WatchdogService(
        store, secrets, probe_channel, adapter
    )
    remote_store = RemoteStore(base_path / "watchdog.db")
    remote_store.initialize()
    tunnel = FrpTunnelManager.from_base_path(base_path)
    event_hub = RemoteEventHub(
        lambda: {
            session["threadId"] for session in remote_store.list_synced_sessions()
        }
    )
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
        tunnel_status_provider=tunnel.status,
    )
    return ConsoleRuntime(
        WatchdogHttpApi(application),
        scheduler,
        adapter,
        AdminRemoteApi(remote_application),
        RemoteHttpApi(remote_application),
        tunnel,
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

    def respond_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
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
            self.respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "Remote workspace is unavailable."},
            )
            return True
        response = REMOTE_ADMIN_API.dispatch(method, path, payload)
        if response is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
        else:
            self.respond_remote_response(response)
        return True

    def dispatch_watchdog(
        self, method: str, parsed: object, payload: object
    ) -> bool:
        path = parsed.path
        if path != "/api/watchdog" and not path.startswith("/api/watchdog/"):
            return False
        if WATCHDOG_API is None:
            self.respond_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "Watchdog runtime is unavailable."},
            )
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
        if self.dispatch_watchdog("GET", parsed, None):
            return
        if self.dispatch_remote_admin("GET", self.path, None):
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
            self.respond_file(ROOT / "remote.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/remote.css":
            self.respond_file(ROOT / "remote.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/remote.js":
            self.respond_file(ROOT / "remote.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/assets/vendor/qrcode.min.js":
            self.respond_file(ROOT / "assets" / "vendor" / "qrcode.min.js", "text/javascript; charset=utf-8")
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
        if length > 256_000:
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
            "/remote.css": ("remote.css", "text/css; charset=utf-8", "public, max-age=3600"),
            "/remote.js": ("remote.js", "text/javascript; charset=utf-8", "public, max-age=3600"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", "no-cache"),
            "/service-worker.js": ("service-worker.js", "text/javascript; charset=utf-8", "no-cache"),
            "/assets/project-console-icon.png": ("assets/project-console-icon.png", "image/png", "public, max-age=86400"),
            "/assets/vendor/qrcode.min.js": ("assets/vendor/qrcode.min.js", "text/javascript; charset=utf-8", "public, max-age=86400"),
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


def run_console(base_path: Path = ROOT) -> None:
    global WATCHDOG_API, REMOTE_ADMIN_API, REMOTE_HTTP_API
    LOG_DIR.mkdir(exist_ok=True)
    runtime = create_console_runtime(base_path)
    WATCHDOG_API = runtime.api
    REMOTE_ADMIN_API = getattr(runtime, "remote_admin_api", None)
    REMOTE_HTTP_API = getattr(runtime, "remote_http_api", None)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    remote_server = None
    remote_thread = None
    tunnel = getattr(runtime, "tunnel", None)
    if REMOTE_HTTP_API is not None:
        remote_server = ThreadingHTTPServer((REMOTE_HOST, REMOTE_PORT), RemoteHandler)
        remote_thread = threading.Thread(
            target=remote_server.serve_forever,
            name="remote-workspace-http",
            daemon=True,
        )
        remote_thread.start()
    if tunnel is not None:
        tunnel.start()
    runtime.scheduler.start()
    print(f"Local Project Console is running at http://{HOST}:{PORT}")
    if remote_server is not None:
        print(f"Remote workspace is listening on {REMOTE_HOST}:{REMOTE_PORT}")
    try:
        server.serve_forever()
    finally:
        runtime.scheduler.stop()
        if tunnel is not None:
            tunnel.stop()
        runtime.adapter.close()
        if remote_server is not None:
            remote_server.shutdown()
            remote_server.server_close()
        if remote_thread is not None:
            remote_thread.join(timeout=2)
        server.server_close()
        WATCHDOG_API = None
        REMOTE_ADMIN_API = None
        REMOTE_HTTP_API = None


if __name__ == "__main__":
    run_console()
