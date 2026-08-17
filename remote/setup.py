from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import re
import shlex
import socket
import ssl
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_FRP_VERSION = "0.61.1"
SERVER_SCRIPT_URL = (
    "https://raw.githubusercontent.com/wk8326-ux/"
    "localhost-project-console/main/scripts/setup-relay-server.sh"
)
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
_SSH_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$", re.I)
_FRP_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][a-z0-9.-]+)?$", re.I)


class RelaySetupError(ValueError):
    pass


@dataclass(frozen=True)
class RelaySetupResponse:
    status: int
    body: object


def _port(value: object, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise RelaySetupError(f"{name}必须是有效端口。") from error
    if not 1 <= port <= 65535:
        raise RelaySetupError(f"{name}必须在 1 到 65535 之间。")
    return port


def _hostname(value: object, name: str, *, require_domain: bool = False) -> str:
    raw = str(value or "").strip().rstrip(".").lower()
    if not raw or len(raw) > 253 or "://" in raw or any(char.isspace() for char in raw):
        raise RelaySetupError(f"{name}无效。")
    try:
        ipaddress.ip_address(raw)
        if require_domain:
            raise RelaySetupError(f"{name}必须是域名，不能填写 IP 地址。")
        return raw
    except ValueError:
        pass
    labels = raw.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise RelaySetupError(f"{name}无效。")
    if require_domain and len(labels) < 2:
        raise RelaySetupError(f"{name}必须是完整域名。")
    return raw


def _ssh_user(value: object) -> str:
    raw = str(value or "ubuntu").strip()
    if not _SSH_USER.fullmatch(raw):
        raise RelaySetupError("SSH 用户名无效。")
    return raw


def _frp_version(value: object) -> str:
    raw = str(value or DEFAULT_FRP_VERSION).strip()
    if not _FRP_VERSION.fullmatch(raw):
        raise RelaySetupError("FRP 版本号无效。")
    return raw


def _normalize_plan(payload: dict) -> dict:
    unknown = set(payload) - {
        "vpsHost",
        "sshUser",
        "domain",
        "frpPort",
        "remotePort",
        "frpVersion",
    }
    if unknown:
        raise RelaySetupError("配置中包含不支持的字段。")
    vps_host = _hostname(payload.get("vpsHost"), "VPS 地址")
    domain = _hostname(payload.get("domain"), "访问域名", require_domain=True)
    return {
        "vpsHost": vps_host,
        "sshUser": _ssh_user(payload.get("sshUser")),
        "domain": domain,
        "frpPort": _port(payload.get("frpPort", 7000), "FRP 端口"),
        "remotePort": _port(payload.get("remotePort", 18766), "远程转发端口"),
        "frpVersion": _frp_version(payload.get("frpVersion")),
        "publicUrl": f"https://{domain}",
    }


def _resolve(host: str) -> list[str]:
    values = {
        item[4][0]
        for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        if item[4]
    }
    return sorted(values, key=lambda item: (":" in item, item))


class RelaySetupService:
    """Builds portable relay plans without persisting FRP credentials."""

    def __init__(
        self,
        base_path: Path,
        *,
        runtime_path: Path | None = None,
        tunnel_status_provider=None,
        tunnel_start_provider=None,
    ) -> None:
        self.base_path = Path(base_path)
        self.runtime_path = (
            Path(runtime_path) if runtime_path is not None else self.base_path / ".runtime"
        )
        self.tunnel_status_provider = tunnel_status_provider
        self.tunnel_start_provider = tunnel_start_provider

    def status(self) -> dict:
        script_path = self.base_path / "scripts" / "setup-remote-client.ps1"
        runtime_path = self.runtime_path / "frp"
        tunnel = self._tunnel_status()
        return {
            "clientScriptReady": script_path.is_file(),
            "clientCommand": (
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
                f'"{script_path}"'
            ),
            "frpcExecutableReady": (runtime_path / "frpc-lpc.exe").is_file(),
            "frpcConfigReady": (runtime_path / "frpc.toml").is_file(),
            "tunnel": tunnel,
        }

    def server_plan(self, payload: dict) -> dict:
        plan = _normalize_plan(payload)
        arguments = [
            "--domain",
            plan["domain"],
            "--server-address",
            plan["vpsHost"],
            "--frp-port",
            str(plan["frpPort"]),
            "--remote-port",
            str(plan["remotePort"]),
            "--frp-version",
            plan["frpVersion"],
        ]
        quoted = " ".join(shlex.quote(item) for item in arguments)
        command = (
            f"curl -fsSL {shlex.quote(SERVER_SCRIPT_URL)} "
            "-o /tmp/lpc-setup-relay.sh && "
            f"sudo bash /tmp/lpc-setup-relay.sh {quoted}"
        )
        return {**plan, "serverCommand": command, "scriptUrl": SERVER_SCRIPT_URL}

    def check_dns(self, payload: dict) -> dict:
        domain = _hostname(payload.get("domain"), "访问域名", require_domain=True)
        expected = _hostname(payload.get("vpsHost"), "VPS 地址")
        try:
            resolved = _resolve(domain)
        except socket.gaierror:
            return {
                "state": "unresolved",
                "matched": False,
                "domain": domain,
                "expected": expected,
                "addresses": [],
                "detail": "DNS 尚未解析，请等待记录生效后重试。",
            }
        try:
            expected_addresses = _resolve(expected)
        except socket.gaierror:
            expected_addresses = [expected]
        matched = bool(set(resolved) & set(expected_addresses))
        return {
            "state": "matched" if matched else "mismatch",
            "matched": matched,
            "domain": domain,
            "expected": expected,
            "addresses": resolved,
            "detail": (
                "域名已经指向目标 VPS。"
                if matched
                else "域名已解析，但结果与填写的 VPS 地址不一致。"
            ),
        }

    def inspect_bundle(self, payload: dict) -> dict:
        bundle = self._decode_bundle(payload.get("bundle"))
        return {
            "valid": True,
            "serverAddr": bundle["serverAddr"],
            "serverPort": bundle["serverPort"],
            "remotePort": bundle["remotePort"],
            "frpVersion": bundle["frpVersion"],
            "publicUrl": bundle["publicUrl"],
        }

    def verify_public_access(self, payload: dict) -> dict:
        raw = str(payload.get("publicUrl") or "").strip().rstrip("/")
        parsed = urlparse(raw)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise RelaySetupError("公网入口必须是 HTTPS 根地址。")
        request = Request(raw, headers={"User-Agent": "LocalProjectConsole/1.0"})
        try:
            with urlopen(request, timeout=6, context=ssl.create_default_context()) as response:
                status = int(response.getcode())
        except HTTPError as error:
            status = int(error.code)
        except (URLError, TimeoutError, OSError) as error:
            return {
                "reachable": False,
                "status": None,
                "publicUrl": raw,
                "detail": f"公网入口暂不可达：{type(error).__name__}",
            }
        reachable = 200 <= status < 500
        return {
            "reachable": reachable,
            "status": status,
            "publicUrl": raw,
            "detail": f"公网入口返回 HTTP {status}。",
        }

    def start_tunnel(self) -> dict:
        if self.tunnel_start_provider is None:
            raise RelaySetupError("当前控制台未提供隧道启动能力。")
        started = bool(self.tunnel_start_provider())
        status = self._tunnel_status()
        if not started and status.get("state") == "not-configured":
            raise RelaySetupError("尚未检测到 frpc 程序和配置文件。")
        return {"started": started, "tunnel": status}

    def _tunnel_status(self) -> dict:
        if self.tunnel_status_provider is None:
            return {
                "provider": "frp",
                "configured": False,
                "running": False,
                "state": "not-configured",
                "detail": "",
            }
        return dict(self.tunnel_status_provider())

    @staticmethod
    def _decode_bundle(value: object) -> dict:
        encoded = str(value or "").strip()
        if not encoded or len(encoded) > 16_384:
            raise RelaySetupError("配置包为空或长度异常。")
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
            decoded = json.loads(raw.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RelaySetupError("配置包格式无效。") from error
        if not isinstance(decoded, dict) or decoded.get("format") != "lpc-frp-v1":
            raise RelaySetupError("配置包版本不受支持。")
        server_addr = _hostname(decoded.get("serverAddr"), "配置包服务器地址")
        public_url = str(decoded.get("publicUrl") or "").strip().rstrip("/")
        parsed = urlparse(public_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise RelaySetupError("配置包中的公网入口无效。")
        token = str(decoded.get("token") or "")
        if not 32 <= len(token) <= 256 or any(char.isspace() for char in token):
            raise RelaySetupError("配置包中的连接凭据无效。")
        return {
            "format": "lpc-frp-v1",
            "serverAddr": server_addr,
            "serverPort": _port(decoded.get("serverPort"), "配置包 FRP 端口"),
            "remotePort": _port(decoded.get("remotePort"), "配置包转发端口"),
            "frpVersion": _frp_version(decoded.get("frpVersion")),
            "publicUrl": public_url,
            "token": token,
        }


class RelaySetupApi:
    def __init__(self, service: RelaySetupService) -> None:
        self.service = service

    def dispatch(self, method: str, path: str, payload: object) -> RelaySetupResponse | None:
        try:
            body = payload if isinstance(payload, dict) else {}
            if method == "GET" and path == "/api/relay-setup/status":
                return RelaySetupResponse(HTTPStatus.OK, self.service.status())
            if method == "POST" and path == "/api/relay-setup/server-plan":
                return RelaySetupResponse(HTTPStatus.OK, self.service.server_plan(body))
            if method == "POST" and path == "/api/relay-setup/dns-check":
                return RelaySetupResponse(HTTPStatus.OK, self.service.check_dns(body))
            if method == "POST" and path == "/api/relay-setup/bundle-check":
                return RelaySetupResponse(HTTPStatus.OK, self.service.inspect_bundle(body))
            if method == "POST" and path == "/api/relay-setup/verify":
                return RelaySetupResponse(
                    HTTPStatus.OK, self.service.verify_public_access(body)
                )
            if method == "POST" and path == "/api/relay-setup/tunnel/start":
                return RelaySetupResponse(HTTPStatus.OK, self.service.start_tunnel())
        except RelaySetupError as error:
            return RelaySetupResponse(HTTPStatus.BAD_REQUEST, {"message": str(error)})
        return None
