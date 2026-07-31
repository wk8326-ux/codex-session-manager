from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


class WatchdogStoreError(RuntimeError):
    """Base error for watchdog persistence failures."""


class ChannelInUseError(WatchdogStoreError):
    """Raised when deleting a channel referenced by a monitored session."""


class _DatabaseConnection(AbstractContextManager[sqlite3.Connection]):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self._connection

    def __exit__(self, exception_type, exception, traceback) -> None:
        try:
            if exception_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()


class WatchdogStore:
    """SQLite boundary for watchdog configuration, audit records, and incidents."""

    _CHANNEL_COLUMNS = {
        "name": "name",
        "baseUrl": "base_url",
        "probeUrlOverride": "probe_url_override",
        "model": "model",
        "encryptedKey": "api_key_ciphertext",
        "timeoutSeconds": "timeout_seconds",
        "enabled": "enabled",
        "lastProbeStatus": "last_probe_status",
        "lastHttpStatus": "last_http_status",
        "lastProbeDetail": "last_probe_detail",
        "lastCheckedAt": "last_checked_at",
        "updatedAt": "updated_at",
    }
    _SESSION_COLUMNS = {
        "name": "name",
        "threadId": "thread_id",
        "hostKind": "host_kind",
        "channelId": "channel_id",
        "intervalMinutes": "interval_minutes",
        "resumePrompt": "resume_prompt",
        "enabled": "enabled",
        "lastSessionState": "last_session_state",
        "lastTurnId": "last_turn_id",
        "lastCheckResult": "last_check_result",
        "lastCheckedAt": "last_checked_at",
        "nextCheckAt": "next_check_at",
        "updatedAt": "updated_at",
    }

    def __init__(self, database_path: Path) -> None:
        self._path = Path(database_path)

    def _connect(self) -> _DatabaseConnection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return _DatabaseConnection(connection)

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchdog_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    default_interval_minutes INTEGER NOT NULL DEFAULT 15,
                    minimum_interval_minutes INTEGER NOT NULL DEFAULT 5,
                    record_retention_days INTEGER NOT NULL DEFAULT 90,
                    record_limit INTEGER NOT NULL DEFAULT 10000,
                    scheduler_enabled INTEGER NOT NULL DEFAULT 1,
                    resume_actions_enabled INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS api_channels (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    probe_url_override TEXT,
                    model TEXT NOT NULL,
                    api_key_ciphertext BLOB NOT NULL,
                    timeout_seconds INTEGER NOT NULL DEFAULT 15,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_probe_status TEXT,
                    last_http_status INTEGER,
                    last_probe_detail TEXT,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS monitored_sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    thread_id TEXT NOT NULL UNIQUE,
                    host_kind TEXT NOT NULL DEFAULT 'local',
                    channel_id TEXT NOT NULL,
                    interval_minutes INTEGER CHECK (interval_minutes IS NULL OR interval_minutes >= 5),
                    resume_prompt TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_session_state TEXT,
                    last_turn_id TEXT,
                    last_check_result TEXT,
                    last_checked_at TEXT,
                    next_check_at TEXT,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (channel_id) REFERENCES api_channels(id)
                );
                CREATE TABLE IF NOT EXISTS recovery_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    match_type TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    description TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS recovery_incidents (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    error_signature TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
                    last_attempt_at TEXT,
                    resolved_at TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES monitored_sessions(id)
                );
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    channel_id TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    channel_status TEXT,
                    http_status INTEGER,
                    session_state TEXT,
                    turn_id TEXT,
                    error_category TEXT,
                    decision TEXT NOT NULL,
                    resume_attempt INTEGER,
                    duration_ms INTEGER,
                    detail_sanitized TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES monitored_sessions(id),
                    FOREIGN KEY (channel_id) REFERENCES api_channels(id)
                );
                CREATE INDEX IF NOT EXISTS monitored_sessions_due
                    ON monitored_sessions(enabled, next_check_at);
                CREATE INDEX IF NOT EXISTS monitor_runs_started
                    ON monitor_runs(started_at);
                """
            )
            if connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                connection.execute("INSERT INTO schema_version(version) VALUES (1)")
            connection.execute(
                """INSERT OR IGNORE INTO watchdog_settings(
                    id, default_interval_minutes, minimum_interval_minutes,
                    record_retention_days, record_limit, scheduler_enabled,
                    resume_actions_enabled
                ) VALUES (1, 15, 5, 90, 10000, 1, 0)"""
            )
            for status in ("429", "502", "503", "504"):
                connection.execute(
                    """INSERT OR IGNORE INTO recovery_rules(
                        id, name, scope, match_type, pattern, enabled, description
                    ) VALUES (?, ?, 'channel', 'http_status', ?, 1, ?)""",
                    (f"http-{status}", f"HTTP {status}", status, "Built-in recoverable status"),
                )
            for pattern, description in (
                ("timeout", "Request timed out"),
                ("connection_reset", "Connection reset"),
            ):
                connection.execute(
                    """INSERT OR IGNORE INTO recovery_rules(
                        id, name, scope, match_type, pattern, enabled, description
                    ) VALUES (?, ?, 'session_turn', 'error_kind', ?, 1, ?)""",
                    (f"builtin-{pattern}", pattern, pattern, description),
                )

    @staticmethod
    def _bool(value: object) -> int:
        return 1 if bool(value) else 0

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        values = dict(row)
        aliases = {
            "base_url": "baseUrl", "probe_url_override": "probeUrlOverride",
            "api_key_ciphertext": "encryptedKey", "timeout_seconds": "timeoutSeconds",
            "last_probe_status": "lastProbeStatus", "last_http_status": "lastHttpStatus",
            "last_probe_detail": "lastProbeDetail", "last_checked_at": "lastCheckedAt",
            "created_at": "createdAt", "updated_at": "updatedAt", "thread_id": "threadId",
            "host_kind": "hostKind", "channel_id": "channelId",
            "interval_minutes": "intervalMinutes", "resume_prompt": "resumePrompt",
            "last_session_state": "lastSessionState", "last_turn_id": "lastTurnId",
            "last_check_result": "lastCheckResult", "next_check_at": "nextCheckAt",
            "match_type": "matchType", "first_seen_at": "firstSeenAt",
            "error_signature": "errorSignature", "attempt_count": "attemptCount",
            "last_attempt_at": "lastAttemptAt", "resolved_at": "resolvedAt",
            "session_id": "sessionId", "started_at": "startedAt", "finished_at": "finishedAt",
            "channel_status": "channelStatus", "http_status": "httpStatus",
            "session_state": "sessionState", "turn_id": "turnId", "error_category": "errorCategory",
            "resume_attempt": "resumeAttempt", "duration_ms": "durationMs",
            "detail_sanitized": "detailSanitized", "default_interval_minutes": "defaultIntervalMinutes",
            "minimum_interval_minutes": "minimumIntervalMinutes", "record_retention_days": "recordRetentionDays",
            "record_limit": "recordLimit", "scheduler_enabled": "schedulerEnabled",
            "resume_actions_enabled": "resumeActionsEnabled",
        }
        for source, target in aliases.items():
            if source in values:
                values[target] = values.pop(source)
        for key in ("enabled", "schedulerEnabled", "resumeActionsEnabled"):
            if key in values:
                values[key] = bool(values[key])
        return values

    def _one(self, query: str, params: tuple = ()) -> dict | None:
        with self._connect() as connection:
            return self._row(connection.execute(query, params).fetchone())

    def get_settings(self) -> dict:
        result = self._one("SELECT * FROM watchdog_settings WHERE id = 1")
        if result is None:
            raise WatchdogStoreError("watchdog settings have not been initialized")
        return result

    def list_recovery_rules(self) -> list[dict]:
        with self._connect() as connection:
            return [
                self._row(row)
                for row in connection.execute(
                    "SELECT * FROM recovery_rules ORDER BY name, id"
                )
            ]

    def update_settings(self, changes: dict) -> dict:
        columns = {
            key: value
            for key, value in changes.items()
            if key in {"defaultIntervalMinutes", "recordRetentionDays", "recordLimit", "schedulerEnabled", "resumeActionsEnabled"}
        }
        if not columns:
            return self.get_settings()
        mapping = {
            "defaultIntervalMinutes": "default_interval_minutes",
            "recordRetentionDays": "record_retention_days", "recordLimit": "record_limit",
            "schedulerEnabled": "scheduler_enabled", "resumeActionsEnabled": "resume_actions_enabled",
        }
        assignments = []
        values = []
        for key, value in columns.items():
            assignments.append(f"{mapping[key]} = ?")
            values.append(self._bool(value) if key.endswith("Enabled") else value)
        with self._connect() as connection:
            connection.execute(f"UPDATE watchdog_settings SET {', '.join(assignments)} WHERE id = 1", values)
        return self.get_settings()

    def create_channel(self, data: dict) -> dict:
        channel_id = str(data.get("id") or uuid4())
        values = (channel_id, data["name"], data["baseUrl"], data.get("probeUrlOverride"), data["model"], data["encryptedKey"], data.get("timeoutSeconds", 15), self._bool(data.get("enabled", True)), data.get("createdAt", ""), data.get("updatedAt", ""))
        with self._connect() as connection:
            connection.execute("""INSERT INTO api_channels(id, name, base_url, probe_url_override, model, api_key_ciphertext, timeout_seconds, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
        return self.get_channel(channel_id)  # type: ignore[return-value]

    def update_channel(self, channel_id: str, changes: dict) -> dict | None:
        filtered = dict(changes)
        if not filtered.get("encryptedKey"):
            filtered.pop("encryptedKey", None)
        return self._update(
            "api_channels", channel_id, filtered, self._CHANNEL_COLUMNS, self.get_channel
        )

    def delete_channel(self, channel_id: str) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM monitored_sessions WHERE channel_id = ? LIMIT 1", (channel_id,)).fetchone():
                raise ChannelInUseError("channel is still referenced by a monitored session")
            connection.execute("DELETE FROM api_channels WHERE id = ?", (channel_id,))

    def list_channels(self) -> list[dict]:
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute("SELECT * FROM api_channels ORDER BY created_at, name")]

    def get_channel(self, channel_id: str) -> dict | None:
        return self._one("SELECT * FROM api_channels WHERE id = ?", (channel_id,))

    def create_session(self, data: dict) -> dict:
        session_id = str(data.get("id") or uuid4())
        values = (session_id, data["name"], data["threadId"], data.get("hostKind", "local"), data["channelId"], data.get("intervalMinutes"), data["resumePrompt"], self._bool(data.get("enabled", True)), data.get("nextCheckAt"), data.get("createdAt", ""), data.get("updatedAt", ""))
        with self._connect() as connection:
            connection.execute("""INSERT INTO monitored_sessions(id, name, thread_id, host_kind, channel_id, interval_minutes, resume_prompt, enabled, next_check_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
        return self.get_session(session_id)  # type: ignore[return-value]

    def update_session(self, session_id: str, changes: dict) -> dict | None:
        return self._update("monitored_sessions", session_id, changes, self._SESSION_COLUMNS, self.get_session)

    def delete_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM monitored_sessions WHERE id = ?", (session_id,))

    def list_sessions(self) -> list[dict]:
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute("SELECT * FROM monitored_sessions ORDER BY created_at, name")]

    def get_session(self, session_id: str) -> dict | None:
        return self._one("SELECT * FROM monitored_sessions WHERE id = ?", (session_id,))

    def _update(self, table: str, record_id: str, changes: dict, allowed: dict[str, str], getter) -> dict | None:
        assignments, values = [], []
        for key, value in changes.items():
            column = allowed.get(key)
            if column is None:
                continue
            assignments.append(f"{column} = ?")
            values.append(self._bool(value) if key == "enabled" else value)
        if not assignments:
            return getter(record_id)
        values.append(record_id)
        with self._connect() as connection:
            connection.execute(f"UPDATE {table} SET {', '.join(assignments)} WHERE id = ?", values)
        return getter(record_id)

    def list_due_sessions(self, now_utc: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT * FROM monitored_sessions WHERE enabled = 1 AND next_check_at IS NOT NULL AND next_check_at <= ? ORDER BY next_check_at, id""", (now_utc,))
            return [self._row(row) for row in rows]

    def set_next_check(self, session_id: str, checked_at: str, next_check_at: str, state: str, result: str, last_turn_id: str | None) -> None:
        with self._connect() as connection:
            connection.execute("""UPDATE monitored_sessions SET last_checked_at = ?, next_check_at = ?, last_session_state = ?, last_check_result = ?, last_turn_id = ? WHERE id = ?""", (checked_at, next_check_at, state, result, last_turn_id, session_id))

    def create_monitor_run(self, data: dict) -> dict:
        run_id = str(data.get("id") or uuid4())
        columns = {"id": run_id, "session_id": data.get("sessionId"), "channel_id": data.get("channelId"), "started_at": data["startedAt"], "finished_at": data.get("finishedAt"), "channel_status": data.get("channelStatus"), "http_status": data.get("httpStatus"), "session_state": data.get("sessionState"), "turn_id": data.get("turnId"), "error_category": data.get("errorCategory"), "decision": data["decision"], "resume_attempt": data.get("resumeAttempt"), "duration_ms": data.get("durationMs"), "detail_sanitized": data.get("detailSanitized", "")}
        with self._connect() as connection:
            connection.execute(f"INSERT INTO monitor_runs({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))
        return self._one("SELECT * FROM monitor_runs WHERE id = ?", (run_id,))  # type: ignore[return-value]

    def list_monitor_runs(self, filters: dict) -> list[dict]:
        clauses, params = [], []
        for key, column in {"sessionId": "session_id", "channelId": "channel_id", "decision": "decision"}.items():
            if filters.get(key):
                clauses.append(f"{column} = ?")
                params.append(filters[key])
        if filters.get("from"):
            clauses.append("started_at >= ?"); params.append(filters["from"])
        if filters.get("to"):
            clauses.append("started_at <= ?"); params.append(filters["to"])
        limit = min(max(int(filters.get("limit", 100)), 1), 500)
        query = "SELECT * FROM monitor_runs" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute(query, params)]

    def get_or_create_incident(self, data: dict) -> dict:
        with self._connect() as connection:
            connection.execute("""INSERT OR IGNORE INTO recovery_incidents(id, fingerprint, session_id, turn_id, error_signature, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)""", (str(data.get("id") or uuid4()), data["fingerprint"], data["sessionId"], data["turnId"], data["errorSignature"], data["firstSeenAt"]))
            return self._row(connection.execute("SELECT * FROM recovery_incidents WHERE fingerprint = ?", (data["fingerprint"],)).fetchone())  # type: ignore[return-value]

    def get_incident(self, fingerprint: str) -> dict | None:
        return self._one("SELECT * FROM recovery_incidents WHERE fingerprint = ?", (fingerprint,))

    def begin_resume_attempt(self, fingerprint: str, attempted_at: str = "") -> dict | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM recovery_incidents WHERE fingerprint = ?", (fingerprint,)).fetchone()
            if row is None or row["status"] in {"sent", "sending", "manual_attention"} or row["attempt_count"] >= 3:
                return self._row(row)
            connection.execute("UPDATE recovery_incidents SET status = 'sending', attempt_count = attempt_count + 1, last_attempt_at = ? WHERE fingerprint = ?", (attempted_at, fingerprint))
            return self._row(connection.execute("SELECT * FROM recovery_incidents WHERE fingerprint = ?", (fingerprint,)).fetchone())

    def mark_incident_sent(self, fingerprint: str, turn_id: str, resolved_at: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE recovery_incidents SET status = 'sent', resolved_at = ?, detail = ? WHERE fingerprint = ?", (resolved_at, f"new turn: {turn_id}", fingerprint))

    def mark_incident_failed(self, fingerprint: str, detail: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE recovery_incidents SET status = CASE WHEN attempt_count >= 3 THEN 'manual_attention' ELSE 'failed' END, detail = ? WHERE fingerprint = ?", (detail, fingerprint))

    def prune_records(self, now_utc: str) -> None:
        settings = self.get_settings()
        current = datetime.strptime(now_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        cutoff = (current - timedelta(days=settings["recordRetentionDays"])).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._connect() as connection:
            connection.execute("DELETE FROM monitor_runs WHERE started_at < ?", (cutoff,))
            extra_rows = connection.execute("SELECT id FROM monitor_runs ORDER BY started_at DESC, id DESC LIMIT -1 OFFSET ?", (settings["recordLimit"],)).fetchall()
            if extra_rows:
                connection.executemany("DELETE FROM monitor_runs WHERE id = ?", [(row["id"],) for row in extra_rows])
            connection.execute("""DELETE FROM recovery_incidents WHERE resolved_at IS NOT NULL AND session_id IN (SELECT id FROM monitored_sessions WHERE last_turn_id IS NOT NULL AND last_turn_id != recovery_incidents.turn_id) AND NOT EXISTS (SELECT 1 FROM monitor_runs WHERE monitor_runs.turn_id = recovery_incidents.turn_id)""")
