"""Codex Session Manager: watchdog automation and remote PWA runtime."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from runtime_paths import APP_VERSION, ApplicationPaths


SOURCE_ROOT = Path(__file__).resolve().parent
PATHS = ApplicationPaths.resolve(SOURCE_ROOT)
ROOT = PATHS.resource_root
HOST = "127.0.0.1"
ADMIN_PORT = int(os.environ.get("CSM_ADMIN_PORT", "8767"))
REMOTE_HOST = os.environ.get("CSM_REMOTE_HOST", "0.0.0.0")
REMOTE_PORT = int(os.environ.get("CSM_REMOTE_PORT", "8766"))
STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")

WATCHDOG_API: object | None = None
REMOTE_ADMIN_API: object | None = None
REMOTE_HTTP_API: object | None = None
RELAY_SETUP_API: object | None = None

# Patchable in tests; production imports the concrete client lazily.
StdioJsonRpcClient = None


class InstanceLock:
    """Hold one cross-process lock for the session manager."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.pid_path = self.path.with_suffix(".pid")
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
            raise RuntimeError("Codex Session Manager is already running.") from error
        self._handle = handle
        pid = str(os.getpid())
        handle.seek(0)
        handle.truncate()
        handle.write(pid.encode("ascii"))
        handle.flush()
        temporary = self.pid_path.with_suffix(".pid.tmp")
        temporary.write_text(pid, encoding="ascii")
        temporary.replace(self.pid_path)

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
            self.pid_path.unlink(missing_ok=True)


@dataclass
class SessionRuntime:
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


def default_remote_base_url() -> str:
    addresses: list[str] = []
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    host = next(
        (address for address in addresses if not address.startswith("127.")),
        "127.0.0.1",
    )
    return f"http://{host}:{REMOTE_PORT}"


def create_runtime(paths: ApplicationPaths) -> SessionRuntime:
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

    store = WatchdogStore(paths.database_path)
    store.initialize()
    # This value is part of the encrypted-data format. Keep it stable so data
    # migrated from Local Project Console remains decryptable for the same user.
    secrets = DpapiSecretStore(entropy=b"localhost-project-console/watchdog/v1")
    remote_store = RemoteStore(paths.database_path)
    remote_store.initialize()
    event_hub = RemoteEventHub(
        lambda: {session["threadId"] for session in remote_store.list_synced_sessions()}
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

    monitor = WatchdogService(store, secrets, probe_channel, adapter)
    tunnel = FrpTunnelManager.from_runtime_path(paths.runtime_root)
    if codex_connected:

        def handle_event(method: str, params: dict) -> None:
            try:
                monitor.handle_app_server_event(method, params)
            finally:
                event_hub.publish(method, params)

        client.set_event_handler(handle_event)

    scheduler = WatchdogScheduler(monitor, store=store)
    application = WatchdogApplication(
        store,
        secrets,
        probe_channel,
        adapter,
        monitor,
        scheduler,
        codex_connected=codex_connected,
    )
    remote_application = RemoteApplication(
        remote_store,
        adapter,
        event_hub,
        lambda: [],
        default_base_url=default_remote_base_url(),
        codex_connected=codex_connected,
        approval_broker=approval_broker,
        tunnel_status_provider=tunnel.status,
        tunnel_start_provider=tunnel.start,
    )
    relay_setup_api = RelaySetupApi(
        RelaySetupService(
            paths.resource_root,
            runtime_path=paths.runtime_root,
            tunnel_status_provider=tunnel.status,
            tunnel_start_provider=tunnel.start,
        )
    )
    native_capture = FlameshotRegionCapture()
    return SessionRuntime(
        WatchdogHttpApi(application),
        scheduler,
        adapter,
        AdminRemoteApi(remote_application, native_capture=native_capture.capture),
        RemoteHttpApi(remote_application),
        tunnel,
        relay_setup_api,
    )


class ResponseHandler(BaseHTTPRequestHandler):
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

    def respond_file(
        self, path: Path, content_type: str, *, cache: str = "no-cache"
    ) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        return value if isinstance(value, dict) else {}


class AdminHandler(ResponseHandler):
    """Local-only administration UI and API on 127.0.0.1:8767."""

    def dispatch(self, method: str, payload: object = None) -> bool:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/watchdog" or path.startswith("/api/watchdog/"):
            response = WATCHDOG_API.dispatch(
                method,
                path,
                parse_qs(parsed.query, keep_blank_values=True),
                payload,
            )
        elif path == "/api/remote" or path.startswith("/api/remote/"):
            response = REMOTE_ADMIN_API.dispatch(method, self.path, payload)
        elif path == "/api/relay-setup" or path.startswith("/api/relay-setup/"):
            response = RELAY_SETUP_API.dispatch(method, path, payload)
        else:
            return False
        if response is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
        elif response.status == HTTPStatus.NO_CONTENT:
            self.respond_empty(response.status)
        else:
            self.respond_json(response.status, response.body)
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/api/health", "/api/auxiliary-health"}:
            self.respond_json(
                HTTPStatus.OK,
                {
                    "service": "codex-session-manager",
                    "version": APP_VERSION,
                    "role": "session-manager",
                    "ready": True,
                    "mode": PATHS.mode,
                    "pid": os.getpid(),
                    "startedAt": STARTED_AT,
                    "ports": {"admin": ADMIN_PORT, "remote": REMOTE_PORT},
                },
            )
            return
        if self.dispatch("GET"):
            return
        static = {
            "/": ("session-manager.html", "text/html; charset=utf-8"),
            "/session-manager": ("session-manager.html", "text/html; charset=utf-8"),
            "/session-manager/": ("session-manager.html", "text/html; charset=utf-8"),
            "/watchdog": ("watchdog.html", "text/html; charset=utf-8"),
            "/watchdog/": ("watchdog.html", "text/html; charset=utf-8"),
            "/remote": ("remote.html", "text/html; charset=utf-8"),
            "/remote/": ("remote.html", "text/html; charset=utf-8"),
            "/remote.css": ("remote.css", "text/css; charset=utf-8"),
            "/remote.js": ("remote.js", "text/javascript; charset=utf-8"),
            "/remote-setup": ("remote-setup.html", "text/html; charset=utf-8"),
            "/remote-setup/": ("remote-setup.html", "text/html; charset=utf-8"),
            "/remote-setup.css": ("remote-setup.css", "text/css; charset=utf-8"),
            "/remote-setup.js": ("remote-setup.js", "text/javascript; charset=utf-8"),
            "/manifest.webmanifest": (
                "manifest.webmanifest",
                "application/manifest+json",
            ),
            "/service-worker.js": (
                "service-worker.js",
                "text/javascript; charset=utf-8",
            ),
            "/assets/session-manager-nav.css": (
                "assets/session-manager-nav.css",
                "text/css; charset=utf-8",
            ),
            "/assets/session-manager-nav.js": (
                "assets/session-manager-nav.js",
                "text/javascript; charset=utf-8",
            ),
            "/assets/session-manager-icon.png": (
                "assets/session-manager-icon.png",
                "image/png",
            ),
            "/assets/vendor/qrcode.min.js": (
                "assets/vendor/qrcode.min.js",
                "text/javascript; charset=utf-8",
            ),
            "/assets/vendor/jsQR.js": (
                "assets/vendor/jsQR.js",
                "text/javascript; charset=utf-8",
            ),
        }
        asset = static.get(path)
        if asset is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
            return
        self.respond_file(ROOT / asset[0], asset[1])

    def _dispatch_body(self, method: str) -> None:
        try:
            payload = self.read_json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self.respond_json(HTTPStatus.BAD_REQUEST, {"message": "请求格式错误。"})
            return
        if not self.dispatch(method, payload):
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})

    def do_POST(self) -> None:
        self._dispatch_body("POST")

    def do_PUT(self) -> None:
        self._dispatch_body("PUT")

    def do_DELETE(self) -> None:
        if not self.dispatch("DELETE"):
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})


class RemoteHandler(ResponseHandler):
    """Authenticated PWA interface on 0.0.0.0:8766."""

    def _headers(self, content_type: str, length: int, *, cache: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def respond_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body), cache="no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_file(
        self, path: Path, content_type: str, *, cache: str = "public, max-age=3600"
    ) -> None:
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
        headers = {key: value for key, value in self.headers.items()}
        response = REMOTE_HTTP_API.dispatch(method, self.path, headers, payload)
        if response is None:
            self.respond_json(HTTPStatus.NOT_FOUND, {"message": "Not found"})
        elif response.status == HTTPStatus.NO_CONTENT:
            self.send_response(response.status)
            self._headers("application/json; charset=utf-8", 0, cache="no-store")
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
            "/manifest.webmanifest": (
                "manifest.webmanifest",
                "application/manifest+json",
                "no-cache",
            ),
            "/service-worker.js": (
                "service-worker.js",
                "text/javascript; charset=utf-8",
                "no-cache",
            ),
            "/assets/session-manager-icon.png": (
                "assets/session-manager-icon.png",
                "image/png",
                "public, max-age=86400",
            ),
            "/assets/vendor/qrcode.min.js": (
                "assets/vendor/qrcode.min.js",
                "text/javascript; charset=utf-8",
                "public, max-age=86400",
            ),
            "/assets/vendor/jsQR.js": (
                "assets/vendor/jsQR.js",
                "text/javascript; charset=utf-8",
                "public, max-age=86400",
            ),
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


def stop_running_instance(paths: ApplicationPaths) -> bool:
    pid_path = paths.runtime_root / "session-manager.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        return result.returncode == 0
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


def run(paths: ApplicationPaths = PATHS) -> None:
    global WATCHDOG_API, REMOTE_ADMIN_API, REMOTE_HTTP_API, RELAY_SETUP_API
    paths.ensure_writable_directories()
    lock = InstanceLock(paths.runtime_root / "session-manager.lock")
    lock.acquire()
    runtime = None
    admin_server = None
    remote_server = None
    remote_thread = None
    tunnel = None
    scheduler_started = False
    try:
        runtime = create_runtime(paths)
        WATCHDOG_API = runtime.api
        REMOTE_ADMIN_API = runtime.remote_admin_api
        REMOTE_HTTP_API = runtime.remote_http_api
        RELAY_SETUP_API = runtime.relay_setup_api
        admin_server = ThreadingHTTPServer((HOST, ADMIN_PORT), AdminHandler)
        remote_server = ThreadingHTTPServer((REMOTE_HOST, REMOTE_PORT), RemoteHandler)
        remote_thread = threading.Thread(
            target=remote_server.serve_forever,
            name="codex-session-manager-remote",
            daemon=True,
        )
        remote_thread.start()
        tunnel = runtime.tunnel
        tunnel.start()
        runtime.scheduler.start()
        scheduler_started = True
        print(f"Codex Session Manager: http://{HOST}:{ADMIN_PORT}")
        print(f"Remote PWA: {REMOTE_HOST}:{REMOTE_PORT}")
        admin_server.serve_forever()
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
        if admin_server is not None:
            admin_server.server_close()
        WATCHDOG_API = None
        REMOTE_ADMIN_API = None
        REMOTE_HTTP_API = None
        RELAY_SETUP_API = None
        lock.release()


def run_service(paths: ApplicationPaths = PATHS) -> None:
    paths.ensure_writable_directories()
    log_path = paths.log_root / "session-manager.log"
    previous = paths.log_root / "session-manager.previous.log"
    if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
        previous.unlink(missing_ok=True)
        log_path.replace(previous)
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        try:
            print(f"[{STARTED_AT}] Starting Codex Session Manager")
            run(paths)
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
        result = migrate_legacy_data(
            Path(arguments[source_index]),
            PATHS,
            replace_existing="--replace-existing" in arguments,
        )
        print(
            json.dumps(
                {
                    "migrated": result.migrated,
                    "source": str(result.source),
                    "destination": str(result.destination),
                    "copied": list(result.copied),
                    "reason": result.reason,
                    "backup": str(result.backup) if result.backup else "",
                },
                ensure_ascii=False,
            )
        )
        return
    if "--stop" in arguments:
        raise SystemExit(0 if stop_running_instance(PATHS) else 1)
    if "--service" in arguments:
        run_service(PATHS)
        return
    run(PATHS)


if __name__ == "__main__":
    main()
