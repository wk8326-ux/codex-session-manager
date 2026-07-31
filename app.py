"""A small local dashboard for starting and monitoring development projects.

Run with: py app.py
Open:     http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "projects.json"
LOG_DIR = ROOT / "logs"
HOST = "127.0.0.1"
PORT = 8765
LOCK = threading.RLock()
WEBSITE_CACHE_LOCK = threading.RLock()
WEBSITE_CACHE: dict[str, dict] = {}
WEBSITE_CACHE_TTL = 15.0
WEBSITE_TIMEOUT = 3.0
WEBSITE_FAILURE_THRESHOLD = 3


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

DEFAULT_PROJECTS = [
    {
        "id": "public-welfare",
        "mode": "local",
        "name": "公益项目管理系统",
        "path": r"D:\WK_workfiles\bot_workspace\projects\public-welfare-lifecycle-system",
        "startCommand": "",
        "stopCommand": "",
        "port": "",
        "url": "",
        "note": "填写项目的本地启动命令后即可控制。",
        "pid": None,
        "startedAt": "",
    },
    {
        "id": "emby-workbench",
        "mode": "local",
        "name": "Emby 刮削工作台",
        "path": r"D:\WK_workfiles\bot_workspace\projects\media-metadata-fixer",
        "startCommand": "",
        "stopCommand": "",
        "port": "",
        "url": "",
        "note": "填写项目的本地启动命令后即可控制。",
        "pid": None,
        "startedAt": "",
    },
    {
        "id": "invoice-tool",
        "mode": "local",
        "name": "发票识别工具",
        "path": r"D:\WK_workfiles\bot_workspace\projects\invoice-registration-assistant",
        "startCommand": "start-invoice-tool.bat",
        "stopCommand": "",
        "port": "",
        "url": "",
        "note": "已检测到启动脚本；建议补充端口和访问链接以获得健康检查。",
        "pid": None,
        "startedAt": "",
    },
    {
        "id": "pt-automation",
        "mode": "external",
        "name": "PT Manager",
        "path": "",
        "startCommand": "",
        "stopCommand": "",
        "port": "",
        "url": "https://ptm.holdzywoo.top/dashboard#overview",
        "note": "服务器常驻服务；通过 Cloudflare Access 验证后打开。",
        "pid": None,
        "startedAt": "",
    },
]


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


def state_for(project: dict, *, website_health: dict | None = None) -> dict:
    external = project.get("mode") == "external"
    external_ready = project_url_is_valid(project.get("url"), external=True)
    pid_active = pid_is_running(project.get("pid"))
    port_active = port_is_open(project.get("port"))
    configured = bool(project.get("startCommand", "").strip())
    health = website_health or {}
    if external and not external_ready:
        state = "needs-config"
        label = "待配置"
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
        "state": state,
        "stateLabel": label,
    }


def states_for(projects: list[dict]) -> list[dict]:
    external_projects = [
        project
        for project in projects
        if project.get("mode") == "external"
        and project_url_is_valid(project.get("url"), external=True)
    ]
    health_by_id: dict[str, dict] = {}
    if external_projects:
        with ThreadPoolExecutor(max_workers=min(4, len(external_projects))) as executor:
            futures = {
                project["id"]: executor.submit(
                    cached_website_health, project.get("url")
                )
                for project in external_projects
            }
            health_by_id = {
                project_id: future.result()
                for project_id, future in futures.items()
            }
    return [
        state_for(project, website_health=health_by_id.get(project.get("id")))
        for project in projects
    ]


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def respond_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/projects":
            with LOCK:
                projects = [dict(project) for project in PROJECTS]
            self.respond_json(HTTPStatus.OK, states_for(projects))
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/log"):
            project_id = parsed.path.split("/")[3]
            log_path = LOG_DIR / f"{project_id}.log"
            content = log_path.read_text(encoding="utf-8", errors="replace")[-12000:] if log_path.exists() else "暂无日志。"
            self.respond_json(HTTPStatus.OK, {"content": content})
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


if __name__ == "__main__":
    LOG_DIR.mkdir(exist_ok=True)
    print(f"Local Project Console is running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
