from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 4


# Retention defaults that shipped before v4. Databases still sitting on these
# exact values were never customized by the user, so the migration below may
# tighten them; any other value is treated as a deliberate choice.
_LEGACY_RETENTION_DAYS = 90
_LEGACY_RECORD_LIMIT = 10000
DEFAULT_RETENTION_DAYS = 14
DEFAULT_RECORD_LIMIT = 2000


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS watchdog_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            default_interval_minutes INTEGER NOT NULL DEFAULT 15,
            minimum_interval_minutes INTEGER NOT NULL DEFAULT 5,
            record_retention_days INTEGER NOT NULL DEFAULT 14,
            record_limit INTEGER NOT NULL DEFAULT 2000,
            scheduler_enabled INTEGER NOT NULL DEFAULT 1,
            resume_actions_enabled INTEGER NOT NULL DEFAULT 0,
            resume_dispatch_mode TEXT NOT NULL DEFAULT 'direct_app_server'
        );
        CREATE TABLE IF NOT EXISTS api_channels (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
            probe_url_override TEXT, model TEXT NOT NULL,
            api_key_ciphertext BLOB NOT NULL,
            timeout_seconds INTEGER NOT NULL DEFAULT 15,
            enabled INTEGER NOT NULL DEFAULT 1, last_probe_status TEXT,
            last_http_status INTEGER, last_probe_detail TEXT, last_checked_at TEXT,
            created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS monitored_sessions (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, thread_id TEXT NOT NULL UNIQUE,
            host_kind TEXT NOT NULL DEFAULT 'local', channel_id TEXT NOT NULL,
            interval_minutes INTEGER CHECK (interval_minutes IS NULL OR interval_minutes >= 5),
            resume_prompt TEXT NOT NULL,
            unattended_approvals_enabled INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1, last_session_state TEXT,
            last_turn_id TEXT, last_check_result TEXT, last_checked_at TEXT,
            next_check_at TEXT, created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (channel_id) REFERENCES api_channels(id)
        );
        CREATE TABLE IF NOT EXISTS recovery_rules (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, scope TEXT NOT NULL,
            match_type TEXT NOT NULL, pattern TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, description TEXT NOT NULL DEFAULT '',
            is_builtin INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS recovery_incidents (
            id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            error_signature TEXT NOT NULL, first_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'candidate',
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
            last_attempt_at TEXT, resolved_at TEXT, detail TEXT NOT NULL DEFAULT '',
            resumed_turn_id TEXT,
            FOREIGN KEY (session_id) REFERENCES monitored_sessions(id)
        );
        CREATE TABLE IF NOT EXISTS monitor_runs (
            id TEXT PRIMARY KEY, session_id TEXT, channel_id TEXT,
            started_at TEXT NOT NULL, finished_at TEXT, channel_status TEXT,
            http_status INTEGER, session_state TEXT, turn_id TEXT,
            resumed_turn_id TEXT, error_category TEXT, decision TEXT NOT NULL,
            resume_attempt INTEGER, duration_ms INTEGER,
            detail_sanitized TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES monitored_sessions(id),
            FOREIGN KEY (channel_id) REFERENCES api_channels(id)
        );
        CREATE TABLE IF NOT EXISTS desktop_bridge_jobs (
            id TEXT PRIMARY KEY, incident_fingerprint TEXT NOT NULL,
            incident_attempt INTEGER NOT NULL, monitor_run_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL, thread_id TEXT NOT NULL, prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', runner_id TEXT, lease_token TEXT,
            lease_expires_at TEXT, claim_attempt_count INTEGER NOT NULL DEFAULT 0,
            resumed_turn_id TEXT, detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, claimed_at TEXT, started_at TEXT, finished_at TEXT,
            UNIQUE (incident_fingerprint, incident_attempt),
            FOREIGN KEY (incident_fingerprint) REFERENCES recovery_incidents(fingerprint),
            FOREIGN KEY (monitor_run_id) REFERENCES monitor_runs(id),
            FOREIGN KEY (session_id) REFERENCES monitored_sessions(id)
        );
        CREATE INDEX IF NOT EXISTS monitored_sessions_due
            ON monitored_sessions(enabled, next_check_at);
        CREATE INDEX IF NOT EXISTS monitor_runs_started ON monitor_runs(started_at);
        CREATE INDEX IF NOT EXISTS desktop_bridge_jobs_pending
            ON desktop_bridge_jobs(status, created_at);
        """
    )
    _ensure_column(
        connection, "monitored_sessions", "unattended_approvals_enabled",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(connection, "recovery_incidents", "resumed_turn_id", "TEXT")
    _ensure_column(connection, "monitor_runs", "resumed_turn_id", "TEXT")
    _ensure_column(
        connection, "watchdog_settings", "resume_dispatch_mode",
        "TEXT NOT NULL DEFAULT 'direct_app_server'",
    )
    _ensure_column(
        connection, "recovery_rules", "is_builtin", "INTEGER NOT NULL DEFAULT 0"
    )
    previous_version = _read_schema_version(connection)
    if previous_version is None:
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        connection.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    connection.execute(
        """INSERT OR IGNORE INTO watchdog_settings(
               id, default_interval_minutes, minimum_interval_minutes,
               record_retention_days, record_limit, scheduler_enabled,
               resume_actions_enabled
           ) VALUES (1, 15, 5, 14, 2000, 1, 0)"""
    )
    _migrate_retention_defaults(connection, previous_version)
    _seed_recovery_rules(connection)


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT version FROM schema_version LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _migrate_retention_defaults(
    connection: sqlite3.Connection, previous_version: int | None
) -> None:
    """Tighten retention only for databases left on the old defaults.

    A 10k-row / 90-day window let silent probes dominate the audit table: the
    live database had 10030 rows, 4678 of them ``silent_channel_unavailable``.
    Rows are pruned only while the scheduler runs, so an untouched default
    eventually costs every record query. Users who already chose their own
    retention keep their numbers.
    """
    # Gated on the schema version so a user who later picks 90/10000 on purpose
    # is not silently reset on the next start.
    if previous_version is not None and previous_version >= 4:
        return
    connection.execute(
        """UPDATE watchdog_settings
              SET record_retention_days = ?, record_limit = ?
            WHERE id = 1
              AND record_retention_days = ?
              AND record_limit = ?""",
        (
            DEFAULT_RETENTION_DAYS,
            DEFAULT_RECORD_LIMIT,
            _LEGACY_RETENTION_DAYS,
            _LEGACY_RECORD_LIMIT,
        ),
    )


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _seed_recovery_rules(connection: sqlite3.Connection) -> None:
    for status in ("429", "502", "503", "504"):
        connection.execute(
            """INSERT OR IGNORE INTO recovery_rules(
                   id, name, scope, match_type, pattern, enabled, description, is_builtin
               ) VALUES (?, ?, 'channel', 'http_status', ?, 1, ?, 1)""",
            (f"http-{status}", f"HTTP {status}", status, "Built-in recoverable status"),
        )
    for pattern, description in (
        ("timeout", "Request timed out"),
        ("connection_reset", "Connection reset"),
    ):
        connection.execute(
            """INSERT OR IGNORE INTO recovery_rules(
                   id, name, scope, match_type, pattern, enabled, description, is_builtin
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
