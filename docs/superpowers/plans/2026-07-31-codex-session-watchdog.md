# Codex Session Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated local Codex session watchdog to the existing console that probes user-configured OpenAI-compatible channels and safely resumes only explicitly monitored sessions whose latest turn failed because of a configured recoverable API error.

**Architecture:** Keep the existing project dashboard behavior intact and add a separate `/watchdog` page plus `/api/watchdog/*` namespace. A focused `watchdog` Python package owns SQLite persistence, DPAPI secrets, channel probes, Codex App Server stdio JSON-RPC, pure decision logic, idempotent recovery incidents, and a scheduler whose lifecycle follows the console process.

**Tech Stack:** Python 3.12 standard library (`sqlite3`, `urllib`, `subprocess`, `threading`, `ctypes`, `unittest`), Codex CLI 0.146+ App Server protocol, vanilla HTML/CSS/JavaScript.

**Design reference:** `docs/superpowers/specs/2026-07-31-codex-session-watchdog-design.md`

---

## Scope And Execution Rules

- The current directory is not a Git repository. Do not initialize Git automatically. Each task contains a Git-aware checkpoint that commits only when the user has initialized Git before execution.
- Use `python -m unittest discover -s tests -v` as the canonical test command. Do not add pytest or third-party runtime packages.
- Keep `projects.json`, `/api/projects`, the five-second project refresh loop, and all existing project start/stop behavior unchanged.
- Automatic resume stays globally disabled through the read-only milestones. It is enabled only after the App Server send test succeeds against a dedicated test thread.
- Never perform the first real send against any active development or production work thread.
- `codex app-server daemon` is unavailable on Windows. `CodexAppServerAdapter` must start `codex app-server --listen stdio://` as a child process and stop it with the console.

## Planned File Structure

```text
localhost-project-console/
├── app.py                         # Existing project API plus thin watchdog routing/lifecycle hooks
├── index.html                     # Existing dashboard; add one 会话监控 sidebar link only
├── watchdog.html                  # Isolated watchdog UI and client logic
├── watchdog.db                    # Runtime data; created on first start, not hand-authored
├── watchdog/
│   ├── __init__.py                # Public construction surface
│   ├── models.py                  # Shared immutable DTOs and enum values
│   ├── codex_adapter.py           # App Server stdio JSON-RPC and normalized thread snapshots
│   ├── secrets.py                 # SecretStore contract and Windows DPAPI implementation
│   ├── validation.py              # Shared URL, interval, UUID, and prompt validation
│   ├── store.py                   # SQLite schema, CRUD, incidents, runs, and migrations
│   ├── channels.py                # OpenAI-compatible probe and response classification
│   ├── decision.py                # Pure state/error decision engine
│   ├── service.py                 # End-to-end monitoring orchestration and retry safety
│   ├── scheduler.py               # Due-time scheduler and clean shutdown
│   └── http_api.py                # `/api/watchdog/*` request validation and response mapping
├── scripts/
│   └── probe_codex_app_server.py  # Read-only protocol diagnostic; explicit opt-in for test send
├── tests/
│   ├── test_app.py                # Existing regression tests
│   ├── test_codex_adapter.py
│   ├── test_watchdog_store.py
│   ├── test_watchdog_secrets.py
│   ├── test_watchdog_validation.py
│   ├── test_watchdog_channels.py
│   ├── test_watchdog_decision.py
│   ├── test_watchdog_service.py
│   ├── test_watchdog_scheduler.py
│   ├── test_watchdog_http_api.py
│   └── test_watchdog_ui.py
└── docs/
    └── watchdog-configuration.md
```

## Task 1: Prove And Encapsulate The Codex App Server Protocol

**Files:**
- Create: `watchdog/__init__.py`
- Create: `watchdog/models.py`
- Create: `watchdog/codex_adapter.py`
- Create: `scripts/probe_codex_app_server.py`
- Test: `tests/test_codex_adapter.py`

- [x] **Step 1: Write failing adapter normalization tests**

Create `watchdog/models.py` imports in the test even though the module does not yet exist:

```python
import unittest

from watchdog.codex_adapter import CodexAppServerAdapter, RpcTransport


class FakeTransport(RpcTransport):
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return self.responses[method]

    def close(self) -> None:
        return


class CodexAdapterTests(unittest.TestCase):
    def test_read_thread_uses_turn_error_status_as_source_of_truth(self) -> None:
        transport = FakeTransport({
            "thread/read": {
                "thread": {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "name": "sample-development-session",
                    "status": {"type": "idle"},
                    "turns": [{
                        "id": "turn-1",
                        "status": "failed",
                        "error": {
                            "message": "upstream unavailable",
                            "codexErrorInfo": {
                                "httpConnectionFailed": {"httpStatusCode": 503}
                            },
                        },
                        "items": [],
                    }],
                }
            }
        })

        snapshot = CodexAppServerAdapter(transport).read_thread(
            "00000000-0000-4000-8000-000000000001"
        )

        self.assertEqual(snapshot.thread_status, "idle")
        self.assertEqual(snapshot.latest_turn.status, "failed")
        self.assertEqual(snapshot.latest_turn.http_status, 503)
        self.assertEqual(transport.calls, [(
            "thread/read",
            {"threadId": "00000000-0000-4000-8000-000000000001", "includeTurns": True},
        )])

    def test_start_turn_sends_one_text_input_without_overriding_thread_settings(self) -> None:
        transport = FakeTransport({
            "thread/resume": {"thread": {"id": "thread-1"}},
            "turn/start": {"turn": {"id": "turn-new", "status": "inProgress", "items": []}}
        })

        turn_id = CodexAppServerAdapter(transport).start_turn(
            "thread-1", "继续当前开发任务"
        )

        self.assertEqual(turn_id, "turn-new")
        self.assertEqual(transport.calls, [
            ("thread/resume", {"threadId": "thread-1"}),
            (
                "turn/start",
                {
                    "threadId": "thread-1",
                    "input": [{
                        "type": "text",
                        "text": "继续当前开发任务",
                        "text_elements": [],
                    }],
                },
            ),
        ])

    def test_turn_start_does_not_override_approval_policy(self) -> None:
        transport = FakeTransport({
            "thread/resume": {"thread": {"id": "thread-1"}},
            "turn/start": {"turn": {"id": "turn-new", "status": "inProgress", "items": []}}
        })
        CodexAppServerAdapter(transport).start_turn("thread-1", "继续")
        for _method, params in transport.calls:
            self.assertNotIn("approvalPolicy", params)
            self.assertNotIn("approvalsReviewer", params)
```

- [x] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m unittest tests.test_codex_adapter -v
```

Expected: import failure for `watchdog.codex_adapter` or missing adapter symbols.

- [x] **Step 3: Add DTOs, strict normalization, and the transport contract**

Implement these public types in `watchdog/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnSnapshot:
    id: str
    status: str
    error_message: str = ""
    error_kind: str = ""
    http_status: int | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    thread_id: str
    name: str
    thread_status: str
    active_flags: tuple[str, ...]
    latest_turn: TurnSnapshot | None
```

Implement `RpcTransport`, `CodexAppServerAdapter.read_thread`, `list_threads`, and `start_turn` in `watchdog/codex_adapter.py`. Normalize only documented App Server values:

```python
class RpcTransport(Protocol):
    def request(self, method: str, params: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _codex_error(error: object) -> tuple[str, int | None]:
    if not isinstance(error, dict):
        return "", None
    info = error.get("codexErrorInfo")
    if not isinstance(info, dict) or len(info) != 1:
        return "", None
    kind, value = next(iter(info.items()))
    status = value.get("httpStatusCode") if isinstance(value, dict) else None
    return str(kind), status if isinstance(status, int) else None
```

Reject a `thread/read` response that lacks `thread`, has a mismatched ID, or has a malformed turn list by raising `CodexProtocolError`. Do not infer recoverability inside the adapter.

- [x] **Step 4: Implement the Windows stdio JSON-RPC client**

Add `StdioJsonRpcClient` that starts:

```python
subprocess.Popen(
    ["codex", "app-server", "--listen", "stdio://"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    bufsize=1,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
```

On start, send exactly:

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"localhost-project-console","title":"Local Project Console","version":"1.0"},"capabilities":null}}
{"method":"initialized"}
```

Use a dedicated reader thread to dispatch response objects by numeric `id`, ignore unrelated server notifications, enforce a request timeout, and fail all pending requests if the child process exits. `close()` must close stdin, terminate only the child process it created, wait up to two seconds, then kill that same PID if necessary.

The reader must also handle server-initiated requests without ever auto-approving work:

```python
SAFE_SERVER_REQUEST_RESULTS = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
}
```

For `item/tool/requestUserInput`, return an `answers` object containing an empty answer list for every supplied question ID. For any other server request, return JSON-RPC error code `-32601`. Every such request must call an `on_attention(thread_id, method)` callback so the service can record “需要人工关注”. Add a transport test that feeds request ID `91` for a command approval and asserts the response is exactly `{"id": 91, "result": {"decision": "decline"}}`; no test or implementation path may emit `accept` or `acceptForSession`.

- [x] **Step 5: Add a diagnostic script with an explicit send gate**

`scripts/probe_codex_app_server.py` must support:

```text
--list --limit 5
--thread-id UUID
--thread-id UUID --allow-send --prompt "watchdog integration test"
```

Without `--allow-send`, the script may call only `thread/list` and `thread/read`. With `--allow-send`, require the exact title `watchdog-integration-test`; refuse all other thread names before calling `thread/resume` or `turn/start`. A successful gate must wait for the created turn and print both `confirmedTurnId` and `"status": "completed"`.

- [x] **Step 6: Verify unit tests and perform the read-only protocol check**

Run:

```powershell
python -m unittest tests.test_codex_adapter -v
python scripts/probe_codex_app_server.py --list --limit 5
```

Expected: tests pass; the script prints at most five local thread IDs, names, and thread statuses without starting a turn.

- [x] **Step 7: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/__init__.py watchdog/models.py watchdog/codex_adapter.py scripts/probe_codex_app_server.py tests/test_codex_adapter.py
  git commit -m "feat: add Codex app server adapter"
}
```

## Task 2: Create The SQLite Store And Seeded Configuration

**Files:**
- Create: `watchdog/store.py`
- Test: `tests/test_watchdog_store.py`

- [x] **Step 1: Write failing schema/default tests**

```python
import tempfile
import unittest
from pathlib import Path

from watchdog.store import WatchdogStore


class WatchdogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WatchdogStore(Path(self.temp.name) / "watchdog.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_and_recovery_rules_are_seeded_once(self) -> None:
        settings = self.store.get_settings()
        self.assertEqual(settings["defaultIntervalMinutes"], 15)
        self.assertEqual(settings["minimumIntervalMinutes"], 5)
        self.assertFalse(settings["resumeActionsEnabled"])
        self.assertEqual(settings["recordRetentionDays"], 90)

        self.store.initialize()
        rules = self.store.list_recovery_rules()
        self.assertEqual({rule["pattern"] for rule in rules if rule["matchType"] == "http_status"}, {"429", "502", "503", "504"})

    def test_due_sessions_returns_only_enabled_due_rows(self) -> None:
        channel = self.store.create_channel({"name": "main", "baseUrl": "https://api.example/v1", "model": "test", "encryptedKey": b"cipher"})
        self.store.create_session({"name": "due", "threadId": "00000000-0000-4000-8000-000000000001", "channelId": channel["id"], "intervalMinutes": 10, "resumePrompt": "继续当前开发任务", "enabled": True, "nextCheckAt": "2026-07-31T06:00:00Z"})
        self.store.create_session({"name": "disabled", "threadId": "00000000-0000-4000-8000-000000000002", "channelId": channel["id"], "intervalMinutes": 10, "resumePrompt": "继续", "enabled": False, "nextCheckAt": "2026-07-31T06:00:00Z"})

        rows = self.store.list_due_sessions("2026-07-31T06:05:00Z")

        self.assertEqual([row["name"] for row in rows], ["due"])

    def test_prune_keeps_incident_while_session_is_still_on_same_turn(self) -> None:
        channel = self.store.create_channel({"name": "main", "baseUrl": "https://api.example/v1", "model": "test", "encryptedKey": b"cipher"})
        session = self.store.create_session({"name": "old", "threadId": "00000000-0000-4000-8000-000000000001", "channelId": channel["id"], "intervalMinutes": 15, "resumePrompt": "继续", "enabled": True, "nextCheckAt": "2026-01-01T00:00:00Z"})
        self.store.create_monitor_run({"sessionId": session["id"], "channelId": channel["id"], "startedAt": "2026-01-01T00:00:00Z", "finishedAt": "2026-01-01T00:00:01Z", "decision": "resume_sent", "turnId": "turn-old"})
        self.store.get_or_create_incident({"fingerprint": "fingerprint-old", "sessionId": session["id"], "turnId": "turn-old", "errorSignature": "httpConnectionFailed:http:503", "firstSeenAt": "2026-01-01T00:00:00Z"})
        self.store.begin_resume_attempt("fingerprint-old")
        self.store.mark_incident_sent("fingerprint-old", "turn-new", "2026-01-01T00:00:01Z")
        self.store.set_next_check(session["id"], "2026-01-01T00:00:01Z", "2026-01-01T00:15:01Z", "failed", "resume_sent", "turn-old")

        self.store.prune_records("2026-07-31T00:00:00Z")

        self.assertEqual(self.store.list_monitor_runs({}), [])
        self.assertIsNotNone(self.store.get_incident("fingerprint-old"))
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_store -v
```

Expected: missing `WatchdogStore`.

- [x] **Step 3: Implement versioned schema creation**

Use one connection per store operation with:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

Create `schema_version`, `watchdog_settings`, `api_channels`, `monitored_sessions`, `recovery_rules`, `recovery_incidents`, and `monitor_runs` exactly as named in the design. Add foreign keys and these critical constraints:

```sql
UNIQUE(monitored_sessions.thread_id)
UNIQUE(recovery_incidents.fingerprint)
CHECK(monitored_sessions.interval_minutes IS NULL OR monitored_sessions.interval_minutes >= 5)
CHECK(recovery_incidents.attempt_count BETWEEN 0 AND 3)
```

Set `resume_actions_enabled` to `0` by default. Use UTC `YYYY-MM-DDTHH:MM:SSZ` strings consistently.

- [x] **Step 4: Implement transactional CRUD and run/incident methods**

Add focused methods rather than exposing SQL:

```python
get_settings()
update_settings(changes)
create_channel(data)
update_channel(channel_id, changes)
delete_channel(channel_id)
list_channels()
get_channel(channel_id)
create_session(data)
update_session(session_id, changes)
delete_session(session_id)
list_sessions()
get_session(session_id)
list_due_sessions(now_utc)
set_next_check(session_id, checked_at, next_check_at, state, result, last_turn_id)
create_monitor_run(data)
list_monitor_runs(filters)
get_or_create_incident(data)
get_incident(fingerprint)
begin_resume_attempt(fingerprint)
mark_incident_sent(fingerprint, turn_id, resolved_at)
mark_incident_failed(fingerprint, detail)
prune_records(now_utc)
```

`delete_channel` must raise `ChannelInUseError` while any session references the channel.
`prune_records` deletes runs older than the configured retention or beyond the configured count. It deletes a resolved incident only after its session has advanced to a different `last_turn_id` and the related runs have expired.

- [x] **Step 5: Verify store tests**

```powershell
python -m unittest tests.test_watchdog_store -v
```

Expected: all store tests pass and no database remains outside the temporary directory.

- [x] **Step 6: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/store.py tests/test_watchdog_store.py
  git commit -m "feat: add watchdog persistence"
}
```

## Task 3: Encrypt Channel Secrets And Validate User Input

**Files:**
- Create: `watchdog/secrets.py`
- Create: `watchdog/validation.py`
- Modify: `watchdog/store.py`
- Test: `tests/test_watchdog_secrets.py`
- Test: `tests/test_watchdog_validation.py`
- Test: `tests/test_watchdog_store.py`

- [x] **Step 1: Write failing SecretStore tests**

```python
import os
import unittest

from watchdog.secrets import DpapiSecretStore, SecretStoreError


@unittest.skipUnless(os.name == "nt", "DPAPI is Windows-specific")
class DpapiSecretStoreTests(unittest.TestCase):
    def test_round_trip_uses_current_user_scope(self) -> None:
        store = DpapiSecretStore(entropy=b"localhost-project-console/watchdog/v1")
        cipher = store.protect("sk-test-value")

        self.assertIsInstance(cipher, bytes)
        self.assertNotIn(b"sk-test-value", cipher)
        self.assertEqual(store.unprotect(cipher), "sk-test-value")

    def test_empty_secret_is_rejected(self) -> None:
        with self.assertRaises(SecretStoreError):
            DpapiSecretStore().protect("")
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_secrets -v
```

Expected: missing `watchdog.secrets`.

- [x] **Step 3: Implement the SecretStore boundary and DPAPI**

Define:

```python
class SecretStore(Protocol):
    def protect(self, value: str) -> bytes:
        raise NotImplementedError

    def unprotect(self, value: bytes) -> str:
        raise NotImplementedError
```

Use `ctypes.windll.crypt32.CryptProtectData` and `CryptUnprotectData` with a `DATA_BLOB` structure, `CRYPTPROTECT_UI_FORBIDDEN`, current-user scope, and optional fixed application entropy. Always release returned buffers with `kernel32.LocalFree`. Wrap platform and Win32 errors in `SecretStoreError` without including the secret.

- [x] **Step 4: Add shared payload validation**

Add pure validation helpers in `watchdog/validation.py`, with these exact rules:

```python
def validate_http_url(value: object) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("渠道地址必须使用有效的 HTTP 或 HTTPS URL。")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("渠道地址不能包含用户名或密码。")
    return raw.rstrip("/")
```

Require non-empty channel name, model, and API Key on create. On update, an omitted/blank API Key preserves the stored ciphertext; a non-empty key replaces it.

Also implement `validate_thread_id` with `uuid.UUID`, `validate_interval` with the five-minute minimum, and `validate_resume_prompt` with non-empty text and a 4000-character maximum. Add direct unit tests for valid and invalid values in `tests/test_watchdog_validation.py`.

- [x] **Step 5: Verify secret and store tests**

```powershell
python -m unittest tests.test_watchdog_secrets tests.test_watchdog_validation tests.test_watchdog_store -v
```

Expected: DPAPI round-trip passes on Windows; no plaintext key appears in serialized channel dictionaries.

- [x] **Step 6: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/secrets.py watchdog/validation.py watchdog/store.py tests/test_watchdog_secrets.py tests/test_watchdog_validation.py tests/test_watchdog_store.py
  git commit -m "feat: protect watchdog channel secrets"
}
```

## Task 4: Probe OpenAI-Compatible Channels And Classify Failures

**Files:**
- Create: `watchdog/channels.py`
- Test: `tests/test_watchdog_channels.py`

- [x] **Step 1: Write failing classification tests with a local HTTP server**

```python
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from watchdog.channels import ChannelConfig, classify_http_status, probe_channel


class ProbeHandler(BaseHTTPRequestHandler):
    status = 200
    body = {"id": "chatcmpl-test", "choices": [{"message": {"content": "ok"}}]}
    received_authorization = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        type(self).received_authorization = self.headers.get("Authorization", "")
        payload = json.dumps(type(self).body).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ChannelProbeTests(unittest.TestCase):
    def test_healthy_probe_calls_real_chat_completion_endpoint(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_channel(ChannelConfig(
                base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                probe_url_override="",
                model="test-model",
                api_key="sk-secret",
                timeout_seconds=2.0,
            ))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

        self.assertEqual(result.category, "healthy")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(ProbeHandler.received_authorization, "Bearer sk-secret")

    def test_http_statuses_are_classified(self) -> None:
        expected = {401: "auth_error", 403: "auth_error", 429: "rate_limited", 502: "upstream_error", 503: "upstream_error", 504: "upstream_error", 418: "other_http_error"}
        for status, category in expected.items():
            with self.subTest(status=status):
                self.assertEqual(classify_http_status(status), category)
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_channels -v
```

- [x] **Step 3: Implement the minimal real-model probe**

Define immutable `ChannelConfig` and `ProbeResult` dataclasses in `watchdog/channels.py`. `ProbeResult` has `category`, `http_status`, `detail`, `duration_ms`, and `checked_at`, plus a derived `healthy` property. Build the default URL by appending `/chat/completions` to the normalized Base URL unless it already ends with that path; allow `probe_url_override` to replace it.

Send:

```json
{
  "model": "configured-model",
  "messages": [{"role": "user", "content": "Reply with OK."}],
  "max_tokens": 1,
  "stream": false
}
```

Treat only 2xx plus valid JSON containing a non-empty `id` or `choices` array as `healthy`. Classify invalid JSON/shape as `protocol_error`; `URLError`, timeout, TLS, DNS, and connection reset as `network_error`. Return sanitized detail and duration; never include Authorization or response bodies in detail.

- [x] **Step 4: Add timeout, invalid JSON, and connection-reset tests**

Use the same local server plus injected opener/clock seams. Assert that every non-healthy result has `healthy == False` and never returns the API key in `detail`.

- [x] **Step 5: Verify tests**

```powershell
python -m unittest tests.test_watchdog_channels -v
```

- [x] **Step 6: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/channels.py tests/test_watchdog_channels.py
  git commit -m "feat: probe monitored API channels"
}
```

## Task 5: Implement The Pure Session Decision Engine

**Files:**
- Create: `watchdog/decision.py`
- Modify: `watchdog/models.py`
- Test: `tests/test_watchdog_decision.py`

- [x] **Step 1: Write the full failing decision matrix**

```python
import unittest

from watchdog.decision import DecisionInput, decide
from watchdog.models import SessionSnapshot, TurnSnapshot


class DecisionTests(unittest.TestCase):
    def test_channel_failure_short_circuits_before_session_read(self) -> None:
        result = decide(DecisionInput(channel_category="upstream_error", snapshot=None, recovery_rules=[]))
        self.assertEqual(result.code, "silent_channel_unavailable")

    def test_completed_and_active_turns_are_silent(self) -> None:
        for status, expected in (("completed", "silent_session_completed"), ("inProgress", "silent_session_running")):
            with self.subTest(status=status):
                snapshot = SessionSnapshot("t", "name", "active", (), TurnSnapshot("turn", status))
                self.assertEqual(decide(DecisionInput("healthy", snapshot, [])).code, expected)

    def test_failed_503_is_resume_candidate(self) -> None:
        snapshot = SessionSnapshot("t", "name", "idle", (), TurnSnapshot("turn", "failed", "unavailable", "httpConnectionFailed", 503))
        rules = [{"matchType": "http_status", "pattern": "503", "enabled": True}]
        result = decide(DecisionInput("healthy", snapshot, rules))
        self.assertEqual(result.code, "resume_candidate")
        self.assertEqual(result.error_signature, "httpConnectionFailed:http:503")

    def test_interrupted_without_error_and_manual_wait_are_never_resumed(self) -> None:
        interrupted = SessionSnapshot("t", "name", "idle", (), TurnSnapshot("turn", "interrupted"))
        waiting = SessionSnapshot("t", "name", "active", ("waitingOnUserInput",), TurnSnapshot("turn", "inProgress"))
        self.assertEqual(decide(DecisionInput("healthy", interrupted, [])).code, "silent_unknown")
        self.assertEqual(decide(DecisionInput("healthy", waiting, [])).code, "silent_manual_attention")
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_decision -v
```

- [x] **Step 3: Implement decision values and exact precedence**

Use this precedence:

```text
channel not healthy
snapshot missing / adapter unavailable
waitingOnApproval or waitingOnUserInput
latest turn missing
inProgress
completed
interrupted without reliable error
failed with enabled recovery rule
failed without recovery rule
```

Use a frozen `Decision` dataclass with `code`, `error_signature`, and `detail`. Match enabled rules by exact HTTP status, exact Codex error kind, or compiled safe regex over the error message. A regex compile failure disables only that rule and returns it in validation results; it must not crash monitoring.

- [x] **Step 4: Add exhaustive strictness tests**

Cover 429/502/503/504, timeout and reset message rules, disabled rules, malformed regex, `systemError`, `notLoaded`, missing turn, and unknown status. Assert that only explicit enabled rule matches produce `resume_candidate`.

- [x] **Step 5: Verify tests**

```powershell
python -m unittest tests.test_watchdog_decision -v
```

- [x] **Step 6: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/models.py watchdog/decision.py tests/test_watchdog_decision.py
  git commit -m "feat: decide safe session recovery"
}
```

## Task 6: Build The Read-Only Monitoring Service

**Files:**
- Create: `watchdog/service.py`
- Modify: `watchdog/store.py`
- Test: `tests/test_watchdog_service.py`

- [x] **Step 1: Write failing short-circuit and audit tests**

Add these complete fixtures above the test class in `tests/test_watchdog_service.py`:

```python
import tempfile
from pathlib import Path

from watchdog.channels import ProbeResult
from watchdog.models import SessionSnapshot, TurnSnapshot
from watchdog.secrets import SecretStore
from watchdog.service import WatchdogService
from watchdog.store import WatchdogStore


class MemorySecretStore(SecretStore):
    def protect(self, value: str) -> bytes:
        return value.encode("utf-8")

    def unprotect(self, value: bytes) -> str:
        return value.decode("utf-8")


class FakeProbe:
    def __init__(self, category: str, http_status: int | None) -> None:
        self.result = ProbeResult(category, http_status, category, 1, "2026-07-31T06:00:00Z")
        self.calls: list[object] = []

    def __call__(self, config: object) -> ProbeResult:
        self.calls.append(config)
        return self.result


class FakeAdapter:
    def __init__(self, snapshot: SessionSnapshot | None = None, start_results: list[str] | None = None, start_errors: list[Exception] | None = None) -> None:
        self.snapshot = snapshot
        self.start_results = list(start_results or [])
        self.start_errors = list(start_errors or [])
        self.read_calls: list[str] = []
        self.start_calls: list[tuple[str, str]] = []

    def read_thread(self, thread_id: str) -> SessionSnapshot:
        self.read_calls.append(thread_id)
        if self.snapshot is None:
            raise AssertionError("read_thread should not have been called")
        return self.snapshot

    def start_turn(self, thread_id: str, prompt: str) -> str:
        self.start_calls.append((thread_id, prompt))
        if self.start_errors:
            raise self.start_errors.pop(0)
        return self.start_results.pop(0)


def failed_snapshot(http_status: int) -> SessionSnapshot:
    return SessionSnapshot(
        "00000000-0000-4000-8000-000000000001",
        "watchdog-test",
        "idle",
        (),
        TurnSnapshot("turn-failed", "failed", "upstream unavailable", "httpConnectionFailed", http_status),
    )


def make_service(probe: FakeProbe, adapter: FakeAdapter, resume_enabled: bool) -> WatchdogService:
    temp = tempfile.TemporaryDirectory()
    store = WatchdogStore(Path(temp.name) / "watchdog.db")
    store.initialize()
    store.update_settings({"resumeActionsEnabled": resume_enabled})
    channel = store.create_channel({"name": "main", "baseUrl": "https://api.example/v1", "model": "test", "encryptedKey": b"sk-test"})
    store.create_session({"id": "session-1", "name": "watchdog-test", "threadId": "00000000-0000-4000-8000-000000000001", "channelId": channel["id"], "intervalMinutes": 15, "resumePrompt": "继续当前开发任务", "enabled": True, "nextCheckAt": "2026-07-31T06:00:00Z"})
    service = WatchdogService(store, MemorySecretStore(), probe, adapter)
    service._test_temp_directory = temp
    return service
```

```python
class ReadOnlyServiceTests(unittest.TestCase):
    def test_unhealthy_channel_never_reads_codex(self) -> None:
        probe = FakeProbe(category="upstream_error", http_status=503)
        adapter = FakeAdapter()
        service = make_service(probe=probe, adapter=adapter, resume_enabled=False)

        run = service.check_session("session-1", now="2026-07-31T06:00:00Z")

        self.assertEqual(run["decision"], "silent_channel_unavailable")
        self.assertEqual(adapter.read_calls, [])
        self.assertEqual(adapter.start_calls, [])

    def test_resume_candidate_is_observed_but_not_sent_in_read_only_mode(self) -> None:
        adapter = FakeAdapter(snapshot=failed_snapshot(503))
        service = make_service(probe=FakeProbe("healthy", 200), adapter=adapter, resume_enabled=False)

        run = service.check_session("session-1", now="2026-07-31T06:00:00Z")

        self.assertEqual(run["decision"], "resume_candidate_observed")
        self.assertEqual(adapter.start_calls, [])
        self.assertEqual(service.store.list_monitor_runs({})[0]["turnId"], "turn-failed")
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_service -v
```

- [x] **Step 3: Implement one-session orchestration**

`WatchdogService.check_session(session_id, now, probe_cache=None)` must:

1. Load enabled session and channel.
2. Decrypt the key only for the probe call.
3. Probe channel and persist its last status.
4. Stop before adapter access unless probe category is `healthy`.
5. Read only the configured thread ID.
6. Run the pure decision engine.
7. If decision is a candidate while `resume_actions_enabled == false`, write `resume_candidate_observed`.
8. Persist a sanitized run and update `next_check_at` in a final transaction.

Never hold a database transaction across a network call.

- [x] **Step 4: Implement batch due checks with per-channel probe reuse**

`run_due(now)` loads due sessions, groups them by `channel_id`, probes each channel once, and passes the same immutable `ProbeResult` to every due session in that group. One session failure must produce its own run and must not stop other sessions.

- [x] **Step 5: Verify service tests**

```powershell
python -m unittest tests.test_watchdog_service -v
```

Expected: two due sessions bound to one channel cause one probe call and two independent run rows.

- [x] **Step 6: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/service.py watchdog/store.py tests/test_watchdog_service.py
  git commit -m "feat: add read-only session monitoring"
}
```

## Task 7: Add Idempotent Incidents And Controlled Resume Attempts

**Files:**
- Modify: `watchdog/models.py`
- Modify: `watchdog/codex_adapter.py`
- Modify: `watchdog/store.py`
- Modify: `watchdog/service.py`
- Test: `tests/test_watchdog_service.py`
- Test: `tests/test_watchdog_store.py`

- [x] **Step 1: Write failing idempotency and retry tests**

```python
class ResumeSafetyTests(unittest.TestCase):
    def test_same_incident_is_sent_once(self) -> None:
        adapter = FakeAdapter(snapshot=failed_snapshot(503), start_results=["turn-new"])
        service = make_service(probe=FakeProbe("healthy", 200), adapter=adapter, resume_enabled=True)

        first = service.check_session("session-1", now="2026-07-31T06:00:00Z")
        second = service.check_session("session-1", now="2026-07-31T06:15:00Z")

        self.assertEqual(first["decision"], "resume_sent")
        self.assertEqual(second["decision"], "silent_already_handled")
        self.assertEqual(len(adapter.start_calls), 1)

    def test_definite_failures_stop_after_three_total_attempts(self) -> None:
        adapter = FakeAdapter(snapshot=failed_snapshot(503), start_errors=[DefiniteSendFailure("busy")] * 3)
        service = make_service(probe=FakeProbe("healthy", 200), adapter=adapter, resume_enabled=True)

        decisions = [service.check_session("session-1", now=value)["decision"] for value in ("2026-07-31T06:00:00Z", "2026-07-31T06:01:00Z", "2026-07-31T06:03:00Z", "2026-07-31T06:30:00Z")]

        self.assertEqual(decisions[-1], "resume_action_failed")
        self.assertEqual(len(adapter.start_calls), 3)

    def test_uncertain_send_result_is_not_blindly_retried(self) -> None:
        adapter = FakeAdapter(snapshot=failed_snapshot(503), start_errors=[UncertainSendFailure("response timeout")])
        service = make_service(probe=FakeProbe("healthy", 200), adapter=adapter, resume_enabled=True)
        service.check_session("session-1", now="2026-07-31T06:00:00Z")
        service.check_session("session-1", now="2026-07-31T06:15:00Z")
        self.assertEqual(len(adapter.start_calls), 1)

    def test_startup_converts_stale_sending_incident_to_manual_attention(self) -> None:
        service = make_service(probe=FakeProbe("healthy", 200), adapter=FakeAdapter(snapshot=failed_snapshot(503)), resume_enabled=True)
        service.store.get_or_create_incident({"fingerprint": "stale", "sessionId": "session-1", "turnId": "turn-failed", "errorSignature": "httpConnectionFailed:http:503", "firstSeenAt": "2026-07-31T05:00:00Z"})
        service.store.begin_resume_attempt("stale")

        service.recover_interrupted_sends("2026-07-31T06:00:00Z")

        self.assertEqual(service.store.get_incident("stale")["status"], "manual_attention")
```

Extend the test module imports for this task with:

```python
from watchdog.codex_adapter import DefiniteSendFailure, UncertainSendFailure
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_service tests.test_watchdog_store -v
```

- [x] **Step 3: Implement incident transactions and fingerprints**

Generate:

```python
fingerprint = hashlib.sha256(
    f"{session_id}\0{turn_id}\0{error_signature}".encode("utf-8")
).hexdigest()
```

`begin_resume_attempt` must atomically insert-or-read the incident, refuse `sent`, `sending`, `manual_attention`, and `attempt_count >= 3`, then increment the attempt and mark `sending` in the same `BEGIN IMMEDIATE` transaction.

- [x] **Step 4: Separate definite from uncertain send failures**

The stdio client raises:

```python
class DefiniteSendFailure(CodexAdapterError):
    """The request was rejected before a new turn was confirmed."""


class UncertainSendFailure(CodexAdapterError):
    """The request may have reached Codex but no response was observed."""
```

Explicit JSON-RPC errors are definite. EOF/timeout after the request was written is uncertain. Uncertain results mark the incident `manual_attention` and are never automatically retried.

At service startup, `recover_interrupted_sends` converts every persisted `sending` incident to `manual_attention`. A process crash makes delivery outcome uncertain, so these rows must never return to the automatic retry queue.

- [x] **Step 5: Add 30-second and 120-second retry eligibility**

The first failure schedules attempt 2 after 30 seconds; the second schedules attempt 3 after 120 seconds. Before each retry, reuse a healthy probe only if it is at most 60 seconds old; otherwise probe again. A successful `turn/start` response marks the incident `sent` immediately.

- [x] **Step 6: Verify concurrency**

Run two `check_session` calls simultaneously with a barrier in `FakeAdapter.start_turn`; assert only one enters the adapter and the other records `silent_already_handled` or `sending_in_progress`.

```powershell
python -m unittest tests.test_watchdog_service tests.test_watchdog_store -v
```

- [x] **Step 7: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/models.py watchdog/codex_adapter.py watchdog/store.py watchdog/service.py tests/test_watchdog_service.py tests/test_watchdog_store.py
  git commit -m "feat: safely resume interrupted sessions"
}
```

## Task 8: Run Checks On A Stoppable Scheduler

**Files:**
- Create: `watchdog/scheduler.py`
- Test: `tests/test_watchdog_scheduler.py`

- [x] **Step 1: Write failing lifecycle tests**

```python
import threading
import unittest

from watchdog.scheduler import WatchdogScheduler


class FakeService:
    def __init__(self, errors: list[Exception | None] | None = None) -> None:
        self.errors = list(errors or [])
        self.called = threading.Event()
        self.calls = 0

    def run_due(self, now: str) -> None:
        self.calls += 1
        self.called.set()
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error

    def wait_for_calls(self, count: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.calls >= count:
                return True
            time.sleep(0.01)
        return False


class SchedulerTests(unittest.TestCase):
    def test_start_is_idempotent_and_stop_joins_worker(self) -> None:
        service = FakeService()
        scheduler = WatchdogScheduler(service, poll_seconds=0.01)

        scheduler.start(); scheduler.start()
        service.called.wait(timeout=1)
        scheduler.stop(timeout=1)

        self.assertFalse(scheduler.is_running)
        self.assertEqual(scheduler.worker_count_created, 1)

    def test_service_failure_does_not_kill_scheduler(self) -> None:
        service = FakeService(errors=[RuntimeError("one cycle"), None])
        scheduler = WatchdogScheduler(service, poll_seconds=0.01)
        scheduler.start()
        self.assertTrue(service.wait_for_calls(2, timeout=1))
        scheduler.stop(timeout=1)
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_scheduler -v
```

- [x] **Step 3: Implement the scheduler**

Use one non-daemon worker thread plus `threading.Event.wait(timeout)` so shutdown interrupts sleep. Each cycle calls `service.run_due(now_utc())`, catches and records cycle-level exceptions, then waits for the configured scheduler poll interval. The store itself decides which sessions are due, so waking early cannot trigger duplicate checks. Never run overlapping cycles. Run `store.prune_records(now_utc())` at most once per local calendar day.

- [x] **Step 4: Verify tests**

```powershell
python -m unittest tests.test_watchdog_scheduler -v
```

- [x] **Step 5: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog/scheduler.py tests/test_watchdog_scheduler.py
  git commit -m "feat: schedule watchdog checks"
}
```

## Task 9: Expose The Namespaced Watchdog HTTP API

**Files:**
- Create: `watchdog/http_api.py`
- Modify: `watchdog/__init__.py`
- Modify: `app.py:9-23`
- Modify: `app.py:436-586`
- Modify: `app.py:589-592`
- Test: `tests/test_watchdog_http_api.py`
- Test: `tests/test_app.py`

- [x] **Step 1: Write failing router tests**

```python
class FakeService:
    def __init__(self) -> None:
        self.check_calls: list[str] = []

    def create_channel(self, payload: dict) -> dict:
        return {"id": "channel-1", "name": payload["name"], "apiKeyMasked": "已保存"}

    def check_session(self, session_id: str) -> dict:
        self.check_calls.append(session_id)
        return {"decision": "silent_session_running"}


class WatchdogApiTests(unittest.TestCase):
    def test_channel_create_never_returns_plaintext_key(self) -> None:
        api = WatchdogHttpApi(FakeService())
        response = api.dispatch("POST", "/api/watchdog/channels", {}, {
            "name": "主兼容渠道",
            "baseUrl": "https://api.example/v1",
            "model": "model-a",
            "apiKey": "sk-secret",
        })
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["apiKeyMasked"], "已保存")
        self.assertNotIn("sk-secret", repr(response.body))

    def test_manual_check_uses_normal_safety_pipeline(self) -> None:
        service = FakeService()
        api = WatchdogHttpApi(service)
        api.dispatch("POST", "/api/watchdog/sessions/session-1/check", {}, {})
        self.assertEqual(service.check_calls, ["session-1"])

    def test_project_routes_are_not_claimed(self) -> None:
        api = WatchdogHttpApi(FakeService())
        self.assertIsNone(api.dispatch("GET", "/api/projects", {}, None))
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_http_api -v
```

- [x] **Step 3: Implement strict route dispatch**

Return an `ApiResponse(status, body)` or `None` when the path is outside `/api/watchdog/`. Implement the routes from the design, including the exact user-facing tab terminology “添加监控渠道” in messages. Validate JSON types, UUID thread IDs, interval minimum, prompt non-empty, URL rules, channel references, pagination limits, and filters.

- [x] **Step 4: Integrate without holding the project lock**

In every `Handler.do_*`, dispatch `/api/watchdog/*` before entering the existing `with LOCK:` project section. Network probes and Codex reads must never hold `LOCK`.

Add a small `respond_api_response` helper and serve:

```python
if parsed.path in {"/watchdog", "/watchdog/"}:
    self.respond_file(ROOT / "watchdog.html", "text/html; charset=utf-8")
    return
```

Construct `WatchdogStore`, `DpapiSecretStore`, adapter, service, API, and scheduler inside a `create_console_runtime(ROOT)` factory. In `__main__`, start the scheduler before `serve_forever()` and stop scheduler/adapter in `finally`.

- [x] **Step 5: Add project regression assertions**

Extend `tests/test_app.py` so requesting `/api/projects` with a fake watchdog API still returns project state and `/` still serves `index.html`. Assert watchdog dispatch is not called for project CRUD methods.

- [x] **Step 6: Verify API and regression tests**

```powershell
python -m unittest tests.test_watchdog_http_api tests.test_app -v
```

- [x] **Step 7: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add app.py watchdog/__init__.py watchdog/http_api.py tests/test_watchdog_http_api.py tests/test_app.py
  git commit -m "feat: expose watchdog API"
}
```

## Task 10: Add The Isolated Watchdog Page And Sidebar Entry

**Files:**
- Create: `watchdog.html`
- Modify: `index.html:311-324`
- Test: `tests/test_watchdog_ui.py`

- [x] **Step 1: Write failing static UI contract tests**

```python
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WatchdogUiTests(unittest.TestCase):
    def test_project_page_adds_only_one_watchdog_navigation_link(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('href="/watchdog"'), 1)
        self.assertNotIn("执行记录", html)
        self.assertNotIn("添加监控渠道", html)

    def test_watchdog_page_has_three_horizontal_tabs(self) -> None:
        html = (ROOT / "watchdog.html").read_text(encoding="utf-8")
        labels = ["监控会话", "执行记录", "添加监控渠道"]
        positions = [html.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('class="watchdog-tabs"', html)
```

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_ui -v
```

- [x] **Step 3: Add the sidebar entry without changing filters**

After the existing filter `<nav>` and before `.host-status`, add a visually separate “自动化工具” group with a single anchor to `/watchdog`. Do not add a `data-filter` attribute and do not change filter counts or `state.filter` logic.

- [x] **Step 4: Build the standalone page shell**

Use a separate HTML document with inline CSS/JS, matching the current token system:

```css
:root {
  --canvas: #eef1ef;
  --surface: #f8faf8;
  --ink: #18221f;
  --muted: #68736f;
  --accent: #2d715b;
  --running: #27845a;
  --warning: #ad6b1d;
  --danger: #b44538;
  --radius: 6px;
}
```

Keep the same brand/sidebar shell, highlight “会话监控”, and place `监控会话 / 执行记录 / 添加监控渠道` in a horizontal tablist inside the main page. Include empty, loading, unavailable, disabled, and reduced-motion states. Do not put cards inside cards.

- [x] **Step 5: Verify static tests and responsive layout manually**

```powershell
python -m unittest tests.test_watchdog_ui -v
```

Expected: project page contains only the new entry; watchdog page owns all three subview labels.

- [x] **Step 6: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add index.html watchdog.html tests/test_watchdog_ui.py
  git commit -m "feat: add watchdog workspace"
}
```

## Task 11: Implement Watchdog CRUD, Status, And Audit Interactions

**Files:**
- Modify: `watchdog.html`
- Modify: `watchdog/http_api.py`
- Test: `tests/test_watchdog_http_api.py`
- Test: `tests/test_watchdog_ui.py`

- [x] **Step 1: Add failing response-shape and client-contract tests**

Require these stable response properties:

```json
{
  "status": {"schedulerRunning": true, "codexConnected": true, "nextCheckAt": "2026-07-31T14:40:00Z"},
  "sessions": [{"id": "session-1", "name": "sample-development-session", "state": "running", "effectiveIntervalMinutes": 10}],
  "channels": [{"id": "channel-1", "name": "主兼容渠道", "apiKeyMasked": "已保存", "lastProbeCategory": "healthy"}],
  "runs": [{"decision": "silent_session_running", "detail": "会话正在执行"}]
}
```

Static JS tests must assert that `watchdog.html` uses only `/api/watchdog/` endpoints and never `/api/projects`.

- [x] **Step 2: Verify RED**

```powershell
python -m unittest tests.test_watchdog_http_api tests.test_watchdog_ui -v
```

- [x] **Step 3: Implement the “监控会话” view**

Render enabled count, running, normal idle, and attention metrics. Provide add/edit/delete/enable controls, manual “立即检查并按规则处理”, local Codex selector, channel binding, global/default interval selection, and per-session prompt. The local selector loads only after user action and does not create a monitored row until Save.

- [x] **Step 4: Implement the “执行记录” view**

Add filters for session, channel, decision, and time. Render channel status, HTTP status, turn status, reason, attempt, duration, and timestamp. Use explicit labels “静默”, “已续跑”, “续跑失败”, and “需要关注”. Do not show full prompt or API response body.

- [x] **Step 5: Implement the “添加监控渠道” view**

Provide add/edit/delete/enable, Base URL, model, API Key, advanced probe URL, timeout, and “立即测试”. Keep stored key fields blank on edit and show only “已保存”. A blank key on update means keep existing; it must not clear the key.

- [x] **Step 6: Add optimistic-control safety**

Disable action buttons while requests are pending, preserve table dimensions during refresh, show API validation messages near the relevant dialog, and restore controls after failures. Poll lightweight status/session data at 15 seconds; do not use the project page's five-second polling loop.

Display the global `resumeActionsEnabled` state in the monitor header. Changing it requires a confirmation dialog that explains the dedicated-test-thread prerequisite; disabling it is immediate and never prevents read-only monitoring.

- [x] **Step 7: Verify tests**

```powershell
python -m unittest tests.test_watchdog_http_api tests.test_watchdog_ui -v
```

- [x] **Step 8: Git-aware checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add watchdog.html watchdog/http_api.py tests/test_watchdog_http_api.py tests/test_watchdog_ui.py
  git commit -m "feat: manage monitored sessions and channels"
}
```

## Task 12: Complete Real Integration, Documentation, And End-To-End Verification

**Files:**
- Create: `.gitignore`
- Modify: `README.md`
- Create: `docs/watchdog-configuration.md`
- Modify: `watchdog.html`
- Modify: `tests/test_app.py`
- Modify: any watchdog file only if verification finds an in-scope defect

- [x] **Step 1: Run the entire automated suite before real integration**

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app.py watchdog scripts
```

Expected: all tests pass and compileall exits 0.

- [x] **Step 2: Start the console in read-only monitoring mode**

Confirm `resumeActionsEnabled` remains false, then run:

```powershell
python app.py
```

Expected: `http://127.0.0.1:8765/` and `http://127.0.0.1:8765/watchdog` load; closing the terminal stops both the scheduler and its child App Server process.

- [x] **Step 3: Verify original dashboard regression in a browser**

At desktop and mobile widths, verify project counts, filters, drag ordering, project CRUD, start/stop, logs, and remote website states. Confirm the only new project-page control is the “会话监控” sidebar link.

- [x] **Step 4: Verify watchdog browser flows**

Using Playwright CLI during execution, test desktop 1440x900 and mobile 390x844:

```text
open /watchdog
create a fake/local test channel
run channel probe
open local Codex selector
add a disabled test session
edit interval and prompt
switch tabs
filter execution records
delete the disabled test session
```

Capture screenshots and verify no overlap, horizontal overflow, layout shift, blank canvas, or console errors.

- [x] **Step 5: Perform the dedicated Codex send gate**

Create a disposable local Codex task titled exactly `watchdog-integration-test`. Use the diagnostic script to read it first, then run the explicit `--allow-send` mode with prompt `watchdog integration test`. Confirm the new turn ID appears and the task receives exactly one prompt.

Only after this passes, set `resumeActionsEnabled` to true through the settings API/UI. If the protocol test fails, leave the switch false and report the exact App Server error; do not use a real work session as fallback.

- [x] **Step 6: Verify one simulated recoverable incident end-to-end**

Use the local fake OpenAI-compatible server and fake adapter integration fixture to create a 503 failed turn. Run the check twice and assert:

```text
first run: resume_sent, attempt 1
second run: silent_already_handled, no second turn/start
```

- [x] **Step 7: Write operator documentation**

Document:

- What “添加监控渠道” means and required fields.
- How to find/select a local thread ID.
- Global 15-minute default, per-session override, and five-minute minimum.
- Strict recovery rules and why unknown/manual interruptions stay silent.
- Default prompt and per-session customization.
- DPAPI current-user behavior and backup implications.
- Read-only mode, global resume switch, three total attempts, and manual-attention outcomes.
- Windows child App Server lifecycle and supported Codex CLI version check.
- Execution record retention and cleanup.

Create `.gitignore` if absent and include only project-local runtime artifacts introduced by this work:

```gitignore
.superpowers/
watchdog.db
watchdog.db-shm
watchdog.db-wal
```

- [x] **Step 8: Run final verification**

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app.py watchdog scripts
```

Expected: all tests pass; no plaintext API key appears under the project directory when searching a known test key.

- [x] **Step 9: Git-aware final checkpoint**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add .gitignore README.md docs/watchdog-configuration.md app.py index.html watchdog.html watchdog tests scripts
  git commit -m "docs: document session watchdog"
}
```

## Final Acceptance Checklist

- [x] Existing project dashboard behavior is unchanged except for one sidebar link.
- [x] `/watchdog` has horizontal `监控会话 / 执行记录 / 添加监控渠道` tabs.
- [x] Only explicitly added and enabled sessions are read by the scheduler.
- [x] Multiple channels and per-session binding work.
- [x] Global 15-minute default, per-session override, and five-minute minimum work.
- [x] API-unavailable paths never call the Codex adapter.
- [x] Completed, running, manual, waiting, interrupted-without-error, and unknown sessions never resume.
- [x] Explicit recoverable failed turns create a stable incident fingerprint.
- [x] A successful incident sends exactly one configured prompt.
- [x] Definite send failures stop after three total attempts; uncertain sends do not retry blindly.
- [x] API keys remain DPAPI-encrypted and are never returned or logged.
- [x] Execution records explain every silent/resume decision.
- [x] Scheduler and child App Server stop when the console process exits.
- [x] All automated tests, browser checks, and the dedicated test-thread gate pass.

## Completion Evidence

- `python -m unittest discover -s tests -v`: 108 tests passed.
- `python -m compileall -q app.py watchdog scripts` and `git diff --check`: passed.
- Desktop and mobile browser workflows passed without overflow or console errors.
- The dedicated local Codex send gate created exactly one prompt and one completed turn.
- A simulated recoverable 503 incident resumed once; the second check stayed silent.
- Closing the console with `CTRL_BREAK` stopped its HTTP server and owned App Server child process.
