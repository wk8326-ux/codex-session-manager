from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .channels import ProbeResult


class WatchdogStoreError(RuntimeError):
    """Base error for watchdog persistence failures."""


class ResumeOutcomePersistenceError(WatchdogStoreError):
    """Raised after an atomic resume finalization has been rolled back."""


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
        "unattendedApprovalsEnabled": "unattended_approvals_enabled",
        "enabled": "enabled",
        "lastSessionState": "last_session_state",
        "lastTurnId": "last_turn_id",
        "lastCheckResult": "last_check_result",
        "lastCheckedAt": "last_checked_at",
        "nextCheckAt": "next_check_at",
        "updatedAt": "updated_at",
    }
    _RECOVERY_RULE_COLUMNS = {
        "name": "name",
        "pattern": "pattern",
        "enabled": "enabled",
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
                    resume_actions_enabled INTEGER NOT NULL DEFAULT 0,
                    resume_dispatch_mode TEXT NOT NULL DEFAULT 'direct_app_server'
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
                    unattended_approvals_enabled INTEGER NOT NULL DEFAULT 0,
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
                    description TEXT NOT NULL DEFAULT '',
                    is_builtin INTEGER NOT NULL DEFAULT 0
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
                    resumed_turn_id TEXT,
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
                    resumed_turn_id TEXT,
                    error_category TEXT,
                    decision TEXT NOT NULL,
                    resume_attempt INTEGER,
                    duration_ms INTEGER,
                    detail_sanitized TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (session_id) REFERENCES monitored_sessions(id),
                    FOREIGN KEY (channel_id) REFERENCES api_channels(id)
                );
                CREATE TABLE IF NOT EXISTS desktop_bridge_jobs (
                    id TEXT PRIMARY KEY,
                    incident_fingerprint TEXT NOT NULL,
                    incident_attempt INTEGER NOT NULL,
                    monitor_run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    runner_id TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    claim_attempt_count INTEGER NOT NULL DEFAULT 0,
                    resumed_turn_id TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE (incident_fingerprint, incident_attempt),
                    FOREIGN KEY (incident_fingerprint)
                        REFERENCES recovery_incidents(fingerprint),
                    FOREIGN KEY (monitor_run_id) REFERENCES monitor_runs(id),
                    FOREIGN KEY (session_id) REFERENCES monitored_sessions(id)
                );
                CREATE INDEX IF NOT EXISTS monitored_sessions_due
                    ON monitored_sessions(enabled, next_check_at);
                CREATE INDEX IF NOT EXISTS monitor_runs_started
                    ON monitor_runs(started_at);
                CREATE INDEX IF NOT EXISTS desktop_bridge_jobs_pending
                    ON desktop_bridge_jobs(status, created_at);
                """
            )
            self._ensure_column(
                connection,
                "monitored_sessions",
                "unattended_approvals_enabled",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "recovery_incidents",
                "resumed_turn_id",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "monitor_runs",
                "resumed_turn_id",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "watchdog_settings",
                "resume_dispatch_mode",
                "TEXT NOT NULL DEFAULT 'direct_app_server'",
            )
            self._ensure_column(
                connection,
                "recovery_rules",
                "is_builtin",
                "INTEGER NOT NULL DEFAULT 0",
            )
            if connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                connection.execute("INSERT INTO schema_version(version) VALUES (3)")
            else:
                connection.execute("UPDATE schema_version SET version = 3")
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
                        id, name, scope, match_type, pattern, enabled, description,
                        is_builtin
                    ) VALUES (?, ?, 'channel', 'http_status', ?, 1, ?, 1)""",
                    (f"http-{status}", f"HTTP {status}", status, "Built-in recoverable status"),
                )
            for pattern, description in (
                ("timeout", "Request timed out"),
                ("connection_reset", "Connection reset"),
            ):
                connection.execute(
                    """INSERT OR IGNORE INTO recovery_rules(
                        id, name, scope, match_type, pattern, enabled, description,
                        is_builtin
                    ) VALUES (?, ?, 'session_turn', 'error_kind', ?, 1, ?, 1)""",
                    (f"builtin-{pattern}", pattern, pattern, description),
                )
            connection.execute(
                """UPDATE recovery_rules SET is_builtin = 1
                   WHERE id IN (
                       'http-429', 'http-502', 'http-503', 'http-504',
                       'builtin-timeout', 'builtin-connection_reset'
                   )"""
            )
            self._cancel_unstarted_desktop_bridge_jobs(
                connection,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        existing = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
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
            "unattended_approvals_enabled": "unattendedApprovalsEnabled",
            "last_session_state": "lastSessionState", "last_turn_id": "lastTurnId",
            "last_check_result": "lastCheckResult", "next_check_at": "nextCheckAt",
            "match_type": "matchType", "first_seen_at": "firstSeenAt",
            "error_signature": "errorSignature", "attempt_count": "attemptCount",
            "last_attempt_at": "lastAttemptAt", "resolved_at": "resolvedAt",
            "session_id": "sessionId", "started_at": "startedAt", "finished_at": "finishedAt",
            "channel_status": "channelStatus", "http_status": "httpStatus",
            "session_state": "sessionState", "turn_id": "turnId", "error_category": "errorCategory",
            "resumed_turn_id": "resumedTurnId",
            "resume_attempt": "resumeAttempt", "duration_ms": "durationMs",
            "detail_sanitized": "detailSanitized", "default_interval_minutes": "defaultIntervalMinutes",
            "minimum_interval_minutes": "minimumIntervalMinutes", "record_retention_days": "recordRetentionDays",
            "record_limit": "recordLimit", "scheduler_enabled": "schedulerEnabled",
            "resume_actions_enabled": "resumeActionsEnabled",
            "resume_dispatch_mode": "resumeDispatchMode",
            "is_builtin": "builtIn",
            "incident_fingerprint": "incidentFingerprint",
            "incident_attempt": "incidentAttempt",
            "monitor_run_id": "monitorRunId",
            "runner_id": "runnerId",
            "lease_token": "leaseToken",
            "lease_expires_at": "leaseExpiresAt",
            "claim_attempt_count": "claimAttemptCount",
            "claimed_at": "claimedAt",
        }
        for source, target in aliases.items():
            if source in values:
                values[target] = values.pop(source)
        for key in (
            "enabled",
            "schedulerEnabled",
            "resumeActionsEnabled",
            "unattendedApprovalsEnabled",
            "builtIn",
        ):
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

    def get_recovery_rule(self, rule_id: str) -> dict | None:
        return self._one("SELECT * FROM recovery_rules WHERE id = ?", (rule_id,))

    def create_recovery_rule(self, data: dict) -> dict:
        rule_id = str(data.get("id") or uuid4())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO recovery_rules(
                       id, name, scope, match_type, pattern, enabled,
                       description, is_builtin
                   ) VALUES (?, ?, 'session_turn', 'message_contains', ?, ?, ?, 0)""",
                (
                    rule_id,
                    data["name"],
                    data["pattern"],
                    self._bool(data.get("enabled", True)),
                    data.get("description", "User-defined recoverable error text"),
                ),
            )
        return self.get_recovery_rule(rule_id)  # type: ignore[return-value]

    def update_recovery_rule(self, rule_id: str, changes: dict) -> dict | None:
        current = self.get_recovery_rule(rule_id)
        if current is None:
            return None
        if current["builtIn"]:
            raise WatchdogStoreError("built-in recovery rules are read-only")
        return self._update(
            "recovery_rules",
            rule_id,
            changes,
            self._RECOVERY_RULE_COLUMNS,
            self.get_recovery_rule,
        )

    def delete_recovery_rule(self, rule_id: str) -> None:
        current = self.get_recovery_rule(rule_id)
        if current is None:
            return
        if current["builtIn"]:
            raise WatchdogStoreError("built-in recovery rules cannot be deleted")
        with self._connect() as connection:
            connection.execute("DELETE FROM recovery_rules WHERE id = ?", (rule_id,))

    def update_settings(self, changes: dict) -> dict:
        columns = {
            key: value
            for key, value in changes.items()
            if key in {
                "defaultIntervalMinutes",
                "recordRetentionDays",
                "recordLimit",
                "schedulerEnabled",
                "resumeActionsEnabled",
                "resumeDispatchMode",
            }
        }
        if not columns:
            return self.get_settings()
        mapping = {
            "defaultIntervalMinutes": "default_interval_minutes",
            "recordRetentionDays": "record_retention_days", "recordLimit": "record_limit",
            "schedulerEnabled": "scheduler_enabled", "resumeActionsEnabled": "resume_actions_enabled",
            "resumeDispatchMode": "resume_dispatch_mode",
        }
        assignments = []
        values = []
        for key, value in columns.items():
            assignments.append(f"{mapping[key]} = ?")
            values.append(self._bool(value) if key.endswith("Enabled") else value)
        with self._connect() as connection:
            connection.execute(f"UPDATE watchdog_settings SET {', '.join(assignments)} WHERE id = 1", values)
            if columns.get("resumeDispatchMode") == "direct_app_server":
                self._cancel_unstarted_desktop_bridge_jobs(
                    connection,
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    all_sessions=True,
                    detail=(
                        "desktop bridge dispatch was cancelled after switching "
                        "to direct App Server mode"
                    ),
                )
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
            connection.execute(
                "UPDATE monitor_runs SET channel_id = NULL WHERE channel_id = ?", (channel_id,)
            )
            connection.execute("DELETE FROM api_channels WHERE id = ?", (channel_id,))

    def list_channels(self) -> list[dict]:
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute("SELECT * FROM api_channels ORDER BY created_at, name")]

    def get_channel(self, channel_id: str) -> dict | None:
        return self._one("SELECT * FROM api_channels WHERE id = ?", (channel_id,))

    def record_channel_probe(self, channel_id: str, result: ProbeResult) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE api_channels
                   SET last_probe_status = ?, last_http_status = ?,
                       last_probe_detail = ?, last_checked_at = ?
                   WHERE id = ?""",
                (
                    result.category,
                    result.http_status,
                    result.detail,
                    result.checked_at,
                    channel_id,
                ),
            )

    def create_session(self, data: dict) -> dict:
        session_id = str(data.get("id") or uuid4())
        values = (session_id, data["name"], data["threadId"], data.get("hostKind", "local"), data["channelId"], data.get("intervalMinutes"), data["resumePrompt"], self._bool(data.get("unattendedApprovalsEnabled", False)), self._bool(data.get("enabled", True)), data.get("nextCheckAt"), data.get("createdAt", ""), data.get("updatedAt", ""))
        with self._connect() as connection:
            connection.execute("""INSERT INTO monitored_sessions(id, name, thread_id, host_kind, channel_id, interval_minutes, resume_prompt, unattended_approvals_enabled, enabled, next_check_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", values)
        return self.get_session(session_id)  # type: ignore[return-value]

    def update_session(self, session_id: str, changes: dict) -> dict | None:
        assignments, values = [], []
        for key, value in changes.items():
            column = self._SESSION_COLUMNS.get(key)
            if column is None:
                continue
            assignments.append(f"{column} = ?")
            values.append(
                self._bool(value)
                if key in {"enabled", "unattendedApprovalsEnabled"}
                else value
            )
        with self._connect() as connection:
            if assignments:
                connection.execute(
                    f"UPDATE monitored_sessions SET {', '.join(assignments)} WHERE id = ?",
                    (*values, session_id),
                )
            if "enabled" in changes and not bool(changes["enabled"]):
                cancelled_at = str(changes.get("updatedAt") or "") or datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                self._cancel_unstarted_desktop_bridge_jobs(
                    connection,
                    cancelled_at,
                    session_id=session_id,
                )
            return self._row(
                connection.execute(
                    "SELECT * FROM monitored_sessions WHERE id = ?", (session_id,)
                ).fetchone()
            )

    @staticmethod
    def _cancel_unstarted_desktop_bridge_jobs(
        connection: sqlite3.Connection,
        cancelled_at: str,
        *,
        session_id: str | None = None,
        all_sessions: bool = False,
        detail: str = "monitoring session was disabled before bridge dispatch",
    ) -> int:
        if session_id is not None:
            rows = connection.execute(
                """SELECT id, monitor_run_id, incident_fingerprint, session_id
                   FROM desktop_bridge_jobs
                   WHERE session_id = ? AND status IN ('pending', 'claimed')""",
                (session_id,),
            ).fetchall()
        elif all_sessions:
            rows = connection.execute(
                """SELECT id, monitor_run_id, incident_fingerprint, session_id
                   FROM desktop_bridge_jobs
                   WHERE status IN ('pending', 'claimed')"""
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT job.id, job.monitor_run_id,
                          job.incident_fingerprint, job.session_id
                   FROM desktop_bridge_jobs AS job
                   LEFT JOIN monitored_sessions AS session
                     ON session.id = job.session_id
                   WHERE job.status IN ('pending', 'claimed')
                     AND (session.id IS NULL OR session.enabled = 0)"""
            ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = 'cancelled', detail = ?, finished_at = ?,
                       lease_token = NULL, lease_expires_at = NULL
                   WHERE id = ? AND status IN ('pending', 'claimed')""",
                (detail, cancelled_at, row["id"]),
            )
            connection.execute(
                """UPDATE monitor_runs
                   SET decision = 'resume_cancelled', session_state = 'cancelled',
                       finished_at = COALESCE(finished_at, ?),
                       detail_sanitized = ?
                   WHERE id = ? AND decision = 'resume_queued'""",
                (cancelled_at, detail, row["monitor_run_id"]),
            )
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = 'failed', resolved_at = NULL, detail = ?
                   WHERE fingerprint = ? AND status = 'sending'""",
                (detail, row["incident_fingerprint"]),
            )
            connection.execute(
                """UPDATE monitored_sessions
                   SET last_check_result = 'resume_cancelled'
                   WHERE id = ? AND last_check_result = 'resume_queued'""",
                (row["session_id"],),
            )
        return len(rows)

    def delete_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM desktop_bridge_jobs WHERE session_id = ?", (session_id,)
            )
            connection.execute(
                "DELETE FROM recovery_incidents WHERE session_id = ?", (session_id,)
            )
            connection.execute(
                "UPDATE monitor_runs SET session_id = NULL WHERE session_id = ?", (session_id,)
            )
            connection.execute("DELETE FROM monitored_sessions WHERE id = ?", (session_id,))

    def list_sessions(self) -> list[dict]:
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute("SELECT * FROM monitored_sessions ORDER BY created_at, name")]

    def get_session(self, session_id: str) -> dict | None:
        return self._one("SELECT * FROM monitored_sessions WHERE id = ?", (session_id,))

    def unattended_approvals_enabled(self, thread_id: str) -> bool:
        session = self._one(
            """SELECT unattended_approvals_enabled AS enabled
               FROM monitored_sessions
               WHERE thread_id = ? AND enabled = 1""",
            (thread_id,),
        )
        return bool(session and session["enabled"])

    def _update(self, table: str, record_id: str, changes: dict, allowed: dict[str, str], getter) -> dict | None:
        assignments, values = [], []
        for key, value in changes.items():
            column = allowed.get(key)
            if column is None:
                continue
            assignments.append(f"{column} = ?")
            values.append(
                self._bool(value)
                if key in {"enabled", "unattendedApprovalsEnabled"}
                else value
            )
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
        with self._connect() as connection:
            return self._insert_monitor_run(connection, data)

    def _insert_monitor_run(
        self, connection: sqlite3.Connection, data: dict
    ) -> dict:
        run_id = str(data.get("id") or uuid4())
        columns = {"id": run_id, "session_id": data.get("sessionId"), "channel_id": data.get("channelId"), "started_at": data["startedAt"], "finished_at": data.get("finishedAt"), "channel_status": data.get("channelStatus"), "http_status": data.get("httpStatus"), "session_state": data.get("sessionState"), "turn_id": data.get("turnId"), "resumed_turn_id": data.get("resumedTurnId"), "error_category": data.get("errorCategory"), "decision": data["decision"], "resume_attempt": data.get("resumeAttempt"), "duration_ms": data.get("durationMs"), "detail_sanitized": data.get("detailSanitized", "")}
        connection.execute(f"INSERT INTO monitor_runs({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", tuple(columns.values()))
        return self._row(
            connection.execute(
                "SELECT * FROM monitor_runs WHERE id = ?", (run_id,)
            ).fetchone()
        )  # type: ignore[return-value]

    def record_monitor_result(self, data: dict, next_check_at: str) -> dict:
        with self._connect() as connection:
            return self._record_monitor_result(connection, data, next_check_at)

    def _record_monitor_result(
        self,
        connection: sqlite3.Connection,
        data: dict,
        next_check_at: str,
    ) -> dict:
        session_id = data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise WatchdogStoreError("monitor result requires a session id")
        checked_at = data.get("finishedAt") or data["startedAt"]
        state = data.get("sessionState") or "unknown"
        run = self._insert_monitor_run(connection, data)
        updated = connection.execute(
            """UPDATE monitored_sessions
               SET last_checked_at = ?, next_check_at = ?,
                   last_session_state = ?, last_check_result = ?,
                   last_turn_id = ?
               WHERE id = ?""",
            (
                checked_at,
                next_check_at,
                state,
                data["decision"],
                data.get("resumedTurnId") or data.get("turnId"),
                session_id,
            ),
        )
        if updated.rowcount != 1:
            raise WatchdogStoreError("monitored session no longer exists")
        return run

    def list_monitor_runs(self, filters: dict) -> list[dict]:
        clauses, params = [], []
        for key, column in {"sessionId": "session_id", "channelId": "channel_id", "decision": "decision"}.items():
            if filters.get(key):
                clauses.append(f"{column} = ?")
                params.append(filters[key])
        if filters.get("from"):
            clauses.append("started_at >= ?")
            params.append(filters["from"])
        if filters.get("to"):
            clauses.append("started_at <= ?")
            params.append(filters["to"])
        limit = min(max(int(filters.get("limit", 100)), 1), 500)
        query = "SELECT * FROM monitor_runs" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY started_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [self._row(row) for row in connection.execute(query, params)]

    def queue_desktop_bridge_job(
        self,
        fingerprint: str,
        thread_id: str,
        prompt: str,
        queued_at: str,
        run_data: dict,
        next_check_at: str,
    ) -> dict:
        """Persist the bridge job, audit run, and next schedule atomically."""
        try:
            with self._connect() as connection:
                incident = connection.execute(
                    """SELECT attempt_count FROM recovery_incidents
                       WHERE fingerprint = ? AND status = 'sending'""",
                    (fingerprint,),
                ).fetchone()
                if incident is None:
                    raise WatchdogStoreError(
                        "resume incident is not awaiting bridge dispatch"
                    )
                run = self._record_monitor_result(
                    connection, run_data, next_check_at
                )
                connection.execute(
                    """INSERT INTO desktop_bridge_jobs(
                           id, incident_fingerprint, incident_attempt,
                           monitor_run_id, session_id, thread_id, prompt,
                           status, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        str(uuid4()),
                        fingerprint,
                        incident["attempt_count"],
                        run["id"],
                        run_data["sessionId"],
                        thread_id,
                        prompt,
                        queued_at,
                    ),
                )
                connection.execute(
                    """UPDATE recovery_incidents
                       SET detail = 'waiting for Codex Desktop bridge'
                       WHERE fingerprint = ? AND status = 'sending'""",
                    (fingerprint,),
                )
                return run
        except Exception as error:
            raise ResumeOutcomePersistenceError(
                "desktop bridge queue transaction was rolled back"
            ) from error

    @staticmethod
    def _lease_expiry(claimed_at: str, lease_seconds: int) -> str:
        current = datetime.strptime(claimed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        return (current + timedelta(seconds=lease_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def claim_desktop_bridge_job(
        self,
        runner_id: str,
        claimed_at: str,
        *,
        lease_seconds: int = 90,
    ) -> dict | None:
        lease_expires_at = self._lease_expiry(claimed_at, lease_seconds)
        lease_token = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._cancel_unstarted_desktop_bridge_jobs(connection, claimed_at)
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = 'pending', runner_id = NULL,
                       lease_token = NULL, lease_expires_at = NULL
                   WHERE status = 'claimed' AND lease_expires_at <= ?
                     AND EXISTS (
                         SELECT 1 FROM monitored_sessions AS session
                         WHERE session.id = desktop_bridge_jobs.session_id
                           AND session.enabled = 1
                     )""",
                (claimed_at,),
            )
            row = connection.execute(
                """SELECT job.id
                   FROM desktop_bridge_jobs AS job
                   JOIN monitored_sessions AS session
                     ON session.id = job.session_id
                   WHERE job.status = 'pending' AND session.enabled = 1
                   ORDER BY job.created_at, job.id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = 'claimed', runner_id = ?, lease_token = ?,
                       lease_expires_at = ?, claimed_at = ?,
                       claim_attempt_count = claim_attempt_count + 1
                   WHERE id = ? AND status = 'pending'""",
                (
                    runner_id,
                    lease_token,
                    lease_expires_at,
                    claimed_at,
                    row["id"],
                ),
            )
            return self._row(
                connection.execute(
                    "SELECT * FROM desktop_bridge_jobs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
            )

    def get_desktop_bridge_status(self) -> dict:
        with self._connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """SELECT status, COUNT(*) AS count
                       FROM desktop_bridge_jobs GROUP BY status"""
                )
            }
            last_claimed = connection.execute(
                "SELECT MAX(claimed_at) FROM desktop_bridge_jobs"
            ).fetchone()[0]
        return {
            "pending": counts.get("pending", 0),
            "claimed": counts.get("claimed", 0),
            "started": counts.get("started", 0),
            "terminal": sum(
                counts.get(status, 0)
                for status in (
                    "completed",
                    "failed",
                    "interrupted",
                    "manual_attention",
                    "dispatch_failed",
                    "cancelled",
                )
            ),
            "lastClaimedAt": last_claimed,
        }

    @staticmethod
    def _bridge_job(
        connection: sqlite3.Connection, job_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM desktop_bridge_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise WatchdogStoreError("desktop bridge job does not exist")
        return row

    @staticmethod
    def _bridge_run(
        connection: sqlite3.Connection, monitor_run_id: str
    ) -> dict:
        row = connection.execute(
            "SELECT * FROM monitor_runs WHERE id = ?", (monitor_run_id,)
        ).fetchone()
        result = WatchdogStore._row(row)
        if result is None:
            raise WatchdogStoreError("desktop bridge monitor run does not exist")
        return result

    def mark_desktop_bridge_started(
        self,
        job_id: str,
        lease_token: str,
        resumed_turn_id: str,
        started_at: str,
    ) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._bridge_job(connection, job_id)
            if (
                job["status"] == "started"
                and job["resumed_turn_id"] == resumed_turn_id
            ):
                return self._bridge_run(connection, job["monitor_run_id"])
            if job["status"] != "claimed" or job["lease_token"] != lease_token:
                raise WatchdogStoreError("desktop bridge lease is no longer valid")
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = 'started', resumed_turn_id = ?, started_at = ?,
                       lease_expires_at = NULL
                   WHERE id = ?""",
                (resumed_turn_id, started_at, job_id),
            )
            updated = connection.execute(
                """UPDATE recovery_incidents
                   SET status = 'started', resumed_turn_id = ?,
                       detail = 'resumed turn started by Codex Desktop'
                   WHERE fingerprint = ? AND status = 'sending'""",
                (resumed_turn_id, job["incident_fingerprint"]),
            )
            if updated.rowcount != 1:
                raise WatchdogStoreError(
                    "desktop bridge incident is not awaiting start"
                )
            connection.execute(
                """UPDATE monitor_runs
                   SET decision = 'resume_started', session_state = 'inProgress',
                       resumed_turn_id = ?, finished_at = NULL,
                       detail_sanitized = 'resumed turn started by Codex Desktop'
                   WHERE id = ? AND decision = 'resume_queued'""",
                (resumed_turn_id, job["monitor_run_id"]),
            )
            connection.execute(
                """UPDATE monitored_sessions
                   SET last_session_state = 'inProgress',
                       last_check_result = 'resume_started',
                       last_turn_id = ?, last_checked_at = ?
                   WHERE id = ?""",
                (resumed_turn_id, started_at, job["session_id"]),
            )
            return self._bridge_run(connection, job["monitor_run_id"])

    def finish_desktop_bridge_job(
        self,
        job_id: str,
        lease_token: str,
        outcome: str,
        finished_at: str,
        detail: str = "",
    ) -> dict:
        if outcome == "dispatch_failed":
            return self._finish_desktop_bridge_dispatch_failure(
                job_id, lease_token, finished_at, detail
            )
        outcomes = {
            "completed": ("completed", "resume_completed", "completed"),
            "failed": ("resumed_failed", "resume_failed", "failed"),
            "interrupted": (
                "resumed_interrupted",
                "resume_interrupted",
                "interrupted",
            ),
            "manual_attention": (
                "manual_attention",
                "resume_manual_attention",
                "attention",
            ),
        }
        mapped = outcomes.get(outcome)
        if mapped is None:
            raise WatchdogStoreError("unsupported desktop bridge outcome")
        incident_status, decision, session_state = mapped
        safe_detail = (detail or f"desktop bridge turn {outcome}")[:500]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._bridge_job(connection, job_id)
            if job["status"] == outcome:
                return self._bridge_run(connection, job["monitor_run_id"])
            if job["status"] != "started" or job["lease_token"] != lease_token:
                raise WatchdogStoreError("desktop bridge job is not running")
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = ?, detail = ?, finished_at = ?
                   WHERE id = ?""",
                (outcome, safe_detail, finished_at, job_id),
            )
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = ?, resolved_at = ?, detail = ?
                   WHERE fingerprint = ? AND status = 'started'""",
                (
                    incident_status,
                    finished_at,
                    safe_detail,
                    job["incident_fingerprint"],
                ),
            )
            connection.execute(
                """UPDATE monitor_runs
                   SET decision = ?, session_state = ?, finished_at = ?,
                       detail_sanitized = ?
                   WHERE id = ? AND decision = 'resume_started'""",
                (
                    decision,
                    session_state,
                    finished_at,
                    safe_detail,
                    job["monitor_run_id"],
                ),
            )
            connection.execute(
                """UPDATE monitored_sessions
                   SET last_session_state = ?, last_check_result = ?,
                       last_turn_id = ?, last_checked_at = ?
                   WHERE id = ?""",
                (
                    session_state,
                    decision,
                    job["resumed_turn_id"],
                    finished_at,
                    job["session_id"],
                ),
            )
            return self._bridge_run(connection, job["monitor_run_id"])

    def _finish_desktop_bridge_dispatch_failure(
        self,
        job_id: str,
        lease_token: str,
        finished_at: str,
        detail: str,
    ) -> dict:
        safe_detail = (detail or "Codex Desktop rejected the resume request")[:500]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._bridge_job(connection, job_id)
            if job["status"] == "dispatch_failed":
                return self._bridge_run(connection, job["monitor_run_id"])
            if job["status"] != "claimed" or job["lease_token"] != lease_token:
                raise WatchdogStoreError("desktop bridge job is not awaiting dispatch")
            incident = connection.execute(
                """SELECT attempt_count, last_attempt_at
                   FROM recovery_incidents WHERE fingerprint = ?""",
                (job["incident_fingerprint"],),
            ).fetchone()
            if incident is None:
                raise WatchdogStoreError("desktop bridge incident does not exist")
            exhausted = incident["attempt_count"] >= 3
            retry_delay = {1: 30, 2: 120}.get(incident["attempt_count"])
            retry_at = (
                self._lease_expiry(incident["last_attempt_at"], retry_delay)
                if retry_delay is not None and incident["last_attempt_at"]
                else None
            )
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = 'dispatch_failed', detail = ?, finished_at = ?,
                       lease_expires_at = NULL
                   WHERE id = ?""",
                (safe_detail, finished_at, job_id),
            )
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = ?, resolved_at = ?, detail = ?
                   WHERE fingerprint = ? AND status = 'sending'""",
                (
                    "manual_attention" if exhausted else "failed",
                    finished_at if exhausted else None,
                    safe_detail,
                    job["incident_fingerprint"],
                ),
            )
            connection.execute(
                """UPDATE monitor_runs
                   SET decision = 'resume_action_failed',
                       session_state = 'failed', finished_at = ?,
                       detail_sanitized = ?
                   WHERE id = ? AND decision = 'resume_queued'""",
                (finished_at, safe_detail, job["monitor_run_id"]),
            )
            connection.execute(
                """UPDATE monitored_sessions
                   SET last_session_state = 'failed',
                       last_check_result = 'resume_action_failed',
                       last_checked_at = ?,
                       next_check_at = COALESCE(?, next_check_at)
                   WHERE id = ?""",
                (finished_at, retry_at, job["session_id"]),
            )
            return self._bridge_run(connection, job["monitor_run_id"])

    def get_or_create_incident(self, data: dict) -> dict:
        with self._connect() as connection:
            connection.execute("""INSERT OR IGNORE INTO recovery_incidents(id, fingerprint, session_id, turn_id, error_signature, first_seen_at) VALUES (?, ?, ?, ?, ?, ?)""", (str(data.get("id") or uuid4()), data["fingerprint"], data["sessionId"], data["turnId"], data["errorSignature"], data["firstSeenAt"]))
            return self._row(connection.execute("SELECT * FROM recovery_incidents WHERE fingerprint = ?", (data["fingerprint"],)).fetchone())  # type: ignore[return-value]

    def get_incident(self, fingerprint: str) -> dict | None:
        return self._one("SELECT * FROM recovery_incidents WHERE fingerprint = ?", (fingerprint,))

    def begin_incident(self, data: dict, attempted_at: str) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO recovery_incidents(
                       id, fingerprint, session_id, turn_id, error_signature,
                       first_seen_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(data.get("id") or uuid4()),
                    data["fingerprint"],
                    data["sessionId"],
                    data["turnId"],
                    data["errorSignature"],
                    data["firstSeenAt"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM recovery_incidents WHERE fingerprint = ?",
                (data["fingerprint"],),
            ).fetchone()
            status = row["status"]
            attempt_count = row["attempt_count"]
            if status in {"sent", "started", "completed"}:
                outcome = "already_handled"
            elif status == "sending":
                outcome = "sending_in_progress"
            elif status == "manual_attention" or attempt_count >= 3:
                if status != "manual_attention":
                    connection.execute(
                        """UPDATE recovery_incidents
                           SET status = 'manual_attention'
                           WHERE fingerprint = ?""",
                        (data["fingerprint"],),
                    )
                    row = connection.execute(
                        "SELECT * FROM recovery_incidents WHERE fingerprint = ?",
                        (data["fingerprint"],),
                    ).fetchone()
                outcome = "manual_attention"
            elif status == "failed" and not self._retry_is_due(
                row["last_attempt_at"], attempted_at, attempt_count
            ):
                outcome = "retry_waiting"
            elif status in {"candidate", "failed"}:
                connection.execute(
                    """UPDATE recovery_incidents
                       SET status = 'sending',
                           attempt_count = attempt_count + 1,
                           last_attempt_at = ?
                       WHERE fingerprint = ?""",
                    (attempted_at, data["fingerprint"]),
                )
                row = connection.execute(
                    "SELECT * FROM recovery_incidents WHERE fingerprint = ?",
                    (data["fingerprint"],),
                ).fetchone()
                outcome = "claimed"
            else:
                connection.execute(
                    """UPDATE recovery_incidents
                       SET status = 'manual_attention'
                       WHERE fingerprint = ?""",
                    (data["fingerprint"],),
                )
                row = connection.execute(
                    "SELECT * FROM recovery_incidents WHERE fingerprint = ?",
                    (data["fingerprint"],),
                ).fetchone()
                outcome = "manual_attention"
            result = self._row(row)
            assert result is not None
            result["claimOutcome"] = outcome
            return result

    @staticmethod
    def _retry_is_due(last_attempt_at: str | None, now: str, attempt_count: int) -> bool:
        delay_seconds = {1: 30, 2: 120}.get(attempt_count)
        if delay_seconds is None or not last_attempt_at:
            return False
        try:
            previous = datetime.strptime(
                last_attempt_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            current = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            return False
        return current >= previous + timedelta(seconds=delay_seconds)

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
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = 'sent', resolved_at = ?,
                       detail = 'resume request accepted'
                   WHERE fingerprint = ?""",
                (resolved_at, fingerprint),
            )

    def mark_incident_failed(self, fingerprint: str, detail: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE recovery_incidents SET status = CASE WHEN attempt_count >= 3 THEN 'manual_attention' ELSE 'failed' END, detail = ? WHERE fingerprint = ?", (detail, fingerprint))

    def mark_incident_manual_attention(self, fingerprint: str, resolved_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = 'manual_attention',
                       resolved_at = ?,
                       detail = 'send outcome requires manual confirmation'
                   WHERE fingerprint = ?""",
                (resolved_at, fingerprint),
            )

    def finalize_resume_outcome(
        self,
        fingerprint: str,
        outcome: str,
        resolved_at: str,
        run_data: dict,
        next_check_at: str,
        resumed_turn_id: str | None = None,
    ) -> dict:
        updates = {
            "started": (
                "status = 'started', resolved_at = NULL, resumed_turn_id = ?, "
                "detail = 'resumed turn started'",
                (resumed_turn_id,),
            ),
            "sent": (
                "status = 'sent', resolved_at = ?, "
                "detail = 'resume request accepted'",
                (resolved_at,),
            ),
            "definite_failure": (
                "status = CASE WHEN attempt_count >= 3 "
                "THEN 'manual_attention' ELSE 'failed' END, "
                "resolved_at = CASE WHEN attempt_count >= 3 THEN ? ELSE NULL END, "
                "detail = 'resume request was rejected'",
                (resolved_at,),
            ),
            "manual_attention": (
                "status = 'manual_attention', resolved_at = ?, "
                "detail = 'send outcome requires manual confirmation'",
                (resolved_at,),
            ),
        }
        update = updates.get(outcome)
        if update is None:
            raise WatchdogStoreError("unsupported resume outcome")
        assignment, values = update
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    f"UPDATE recovery_incidents SET {assignment} "
                    "WHERE fingerprint = ? AND status = 'sending'",
                    (*values, fingerprint),
                )
                if cursor.rowcount != 1:
                    raise WatchdogStoreError(
                        "resume incident is not awaiting finalization"
                    )
                return self._record_monitor_result(
                    connection, run_data, next_check_at
                )
        except Exception as error:
            raise ResumeOutcomePersistenceError(
                "resume outcome transaction was rolled back"
            ) from error

    def finalize_resumed_turn(
        self, resumed_turn_id: str, status: str, resolved_at: str
    ) -> bool:
        outcomes = {
            "completed": ("completed", "resume_completed", "resumed turn completed"),
            "failed": ("resumed_failed", "resume_failed", "resumed turn failed"),
            "interrupted": (
                "resumed_interrupted",
                "resume_interrupted",
                "resumed turn was interrupted",
            ),
        }
        outcome = outcomes.get(status)
        if outcome is None:
            return False
        incident_status, decision, detail = outcome
        with self._connect() as connection:
            row = connection.execute(
                """SELECT fingerprint, session_id FROM recovery_incidents
                   WHERE resumed_turn_id = ? AND status = 'started'""",
                (resumed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = ?, resolved_at = ?, detail = ?
                   WHERE fingerprint = ?""",
                (incident_status, resolved_at, detail, row["fingerprint"]),
            )
            connection.execute(
                """UPDATE monitor_runs
                   SET decision = ?, session_state = ?, finished_at = ?,
                       detail_sanitized = ?
                   WHERE resumed_turn_id = ? AND decision IN ('resume_started', 'resume_running')""",
                (decision, status, resolved_at, detail, resumed_turn_id),
            )
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = ?, finished_at = ?, detail = ?
                   WHERE resumed_turn_id = ? AND status = 'started'""",
                (status, resolved_at, detail, resumed_turn_id),
            )
            connection.execute(
                """UPDATE monitored_sessions
                   SET last_session_state = ?, last_check_result = ?,
                       last_turn_id = ?, last_checked_at = ?
                   WHERE id = ?""",
                (status, decision, resumed_turn_id, resolved_at, row["session_id"]),
            )
        return True

    def mark_resumed_turn_manual_attention(
        self, resumed_turn_id: str, detail: str, resolved_at: str
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT fingerprint, session_id FROM recovery_incidents
                   WHERE resumed_turn_id = ? AND status = 'started'""",
                (resumed_turn_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """UPDATE recovery_incidents
                   SET status = 'manual_attention', resolved_at = ?, detail = ?
                   WHERE fingerprint = ?""",
                (resolved_at, detail, row["fingerprint"]),
            )
            connection.execute(
                """UPDATE monitor_runs
                   SET decision = 'resume_manual_attention', finished_at = ?,
                       detail_sanitized = ?
                   WHERE resumed_turn_id = ? AND decision IN ('resume_started', 'resume_running')""",
                (resolved_at, detail, resumed_turn_id),
            )
            connection.execute(
                """UPDATE desktop_bridge_jobs
                   SET status = 'manual_attention', finished_at = ?, detail = ?
                   WHERE resumed_turn_id = ? AND status = 'started'""",
                (resolved_at, detail, resumed_turn_id),
            )
            connection.execute(
                """UPDATE monitored_sessions
                   SET last_check_result = 'resume_manual_attention',
                       last_checked_at = ?
                   WHERE id = ?""",
                (resolved_at, row["session_id"]),
            )
        return True

    def mark_stale_resumed_turns_manual_attention(
        self, session_id: str, latest_turn_id: str, resolved_at: str
    ) -> int:
        detail = "resumed turn outcome was not observed before a newer turn"
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT fingerprint, resumed_turn_id
                   FROM recovery_incidents
                   WHERE session_id = ? AND status = 'started'
                     AND resumed_turn_id IS NOT NULL
                     AND resumed_turn_id != ?
                     AND turn_id != ?""",
                (session_id, latest_turn_id, latest_turn_id),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE recovery_incidents
                       SET status = 'manual_attention', resolved_at = ?, detail = ?
                       WHERE fingerprint = ?""",
                    (resolved_at, detail, row["fingerprint"]),
                )
                connection.execute(
                    """UPDATE monitor_runs
                       SET decision = 'resume_manual_attention', finished_at = ?,
                           detail_sanitized = ?
                       WHERE resumed_turn_id = ?
                         AND decision IN ('resume_started', 'resume_running')""",
                    (resolved_at, detail, row["resumed_turn_id"]),
                )
                connection.execute(
                    """UPDATE desktop_bridge_jobs
                       SET status = 'manual_attention', finished_at = ?, detail = ?
                       WHERE resumed_turn_id = ? AND status = 'started'""",
                    (resolved_at, detail, row["resumed_turn_id"]),
                )
            return len(rows)

    def recover_interrupted_sends(self, recovered_at: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE recovery_incidents
                   SET status = 'manual_attention',
                       resolved_at = ?,
                       detail = 'interrupted send requires manual confirmation'
                   WHERE status = 'sending'
                     AND NOT EXISTS (
                         SELECT 1 FROM desktop_bridge_jobs
                         WHERE desktop_bridge_jobs.incident_fingerprint =
                               recovery_incidents.fingerprint
                           AND desktop_bridge_jobs.status IN ('pending', 'claimed')
                     )""",
                (recovered_at,),
            )
            return cursor.rowcount

    def prune_records(self, now_utc: str) -> None:
        settings = self.get_settings()
        current = datetime.strptime(now_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        cutoff = (current - timedelta(days=settings["recordRetentionDays"])).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with self._connect() as connection:
            aged_rows = connection.execute(
                """SELECT id FROM monitor_runs
                   WHERE finished_at IS NOT NULL AND started_at < ?""",
                (cutoff,),
            ).fetchall()
            self._delete_monitor_runs(connection, aged_rows)
            extra_rows = connection.execute(
                """SELECT id FROM monitor_runs
                   WHERE finished_at IS NOT NULL
                   ORDER BY started_at DESC, id DESC LIMIT -1 OFFSET ?""",
                (settings["recordLimit"],),
            ).fetchall()
            self._delete_monitor_runs(connection, extra_rows)
            connection.execute("""DELETE FROM recovery_incidents WHERE resolved_at IS NOT NULL AND session_id IN (SELECT id FROM monitored_sessions WHERE last_turn_id IS NOT NULL AND last_turn_id != recovery_incidents.turn_id) AND NOT EXISTS (SELECT 1 FROM monitor_runs WHERE monitor_runs.turn_id = recovery_incidents.turn_id)""")

    @staticmethod
    def _delete_monitor_runs(
        connection: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> None:
        if not rows:
            return
        values = [(row["id"],) for row in rows]
        connection.executemany(
            "DELETE FROM desktop_bridge_jobs WHERE monitor_run_id = ?", values
        )
        connection.executemany("DELETE FROM monitor_runs WHERE id = ?", values)
