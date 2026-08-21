from __future__ import annotations

import json
import time
from ipaddress import ip_address
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


@dataclass(frozen=True)
class ChannelConfig:
    base_url: str
    probe_url_override: str
    model: str
    api_key: str
    timeout_seconds: float


@dataclass(frozen=True)
class ProbeResult:
    category: str
    http_status: int | None
    detail: str
    duration_ms: int
    checked_at: str

    @property
    def healthy(self) -> bool:
        return self.category == "healthy"


def classify_http_status(status: int) -> str:
    if 200 <= status < 300:
        return "healthy"
    if status in {401, 403}:
        return "auth_error"
    if status == 429:
        return "rate_limited"
    if status in {502, 503, 504}:
        return "upstream_error"
    return "other_http_error"


def _probe_url(config: ChannelConfig) -> str:
    if config.probe_url_override.strip():
        return config.probe_url_override.strip()
    base = config.base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _is_loopback_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _default_opener(url: str):
    if _is_loopback_url(url):
        return build_opener(ProxyHandler({}))
    return build_opener()


def _checked_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_channel(config: ChannelConfig, *, opener=None, clock=time.perf_counter) -> ProbeResult:
    started = clock()

    def result(category: str, status: int | None, detail: str) -> ProbeResult:
        return ProbeResult(category, status, detail, max(0, round((clock() - started) * 1000)), _checked_at())

    payload = json.dumps({
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    probe_url = _probe_url(config)
    request = Request(
        probe_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "LocalProjectConsole-Watchdog/1.0",
        },
        method="POST",
    )
    client = opener or _default_opener(probe_url)
    try:
        with client.open(request, timeout=config.timeout_seconds) as response:
            status = int(response.getcode())
            body = response.read()
    except HTTPError as error:
        status = int(error.code)
        return result(classify_http_status(status), status, f"HTTP {status}")
    except (URLError, TimeoutError, ConnectionError, OSError) as error:
        return result("network_error", None, f"network request failed ({type(error).__name__})")

    category = classify_http_status(status)
    if category != "healthy":
        return result(category, status, f"HTTP {status}")
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return result("protocol_error", status, "response was not valid JSON")
    if not isinstance(decoded, dict):
        return result("protocol_error", status, "response JSON was not an object")
    response_id = decoded.get("id")
    choices = decoded.get("choices")
    if not ((isinstance(response_id, str) and response_id) or (isinstance(choices, list) and choices)):
        return result("protocol_error", status, "response lacked a completion id or choices")
    return result("healthy", status, "channel responded normally")
