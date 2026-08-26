from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


class RemoteStoreError(RuntimeError):
    """Base error for remote access persistence failures."""


class PairingRejected(RemoteStoreError):
    """A pairing secret is invalid, expired, or already used."""


class MessageDeliveryConflict(RemoteStoreError):
    """A client message ID was reused with different content."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RemoteStore:
    def __init__(
        self,
        database_path: Path,
        *,
        authentication_ttl_seconds: float = 30.0,
        last_seen_write_seconds: float = 60.0,
    ) -> None:
        self._path = Path(database_path)
        self._authentication_ttl = max(0.0, authentication_ttl_seconds)
        self._last_seen_write_seconds = max(0.0, last_seen_write_seconds)
        self._cache_lock = threading.RLock()
        self._authentication_cache: dict[str, tuple[float, dict]] = {}
        self._last_seen_writes: dict[str, float] = {}
        self._synced_sessions_cache: list[dict] | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS remote_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    public_base_url TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS remote_pairings (
                    id TEXT PRIMARY KEY,
                    secret_hash TEXT NOT NULL,
                    comparison_code TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    claimed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS remote_devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS remote_synced_sessions (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_message_deliveries (
                    session_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, client_message_id),
                    FOREIGN KEY(session_id) REFERENCES remote_synced_sessions(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS remote_approval_audit (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '',
                    method TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    actor_device_id TEXT NOT NULL DEFAULT '',
                    actor_device_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS remote_pairings_expiry
                    ON remote_pairings(expires_at, claimed_at);
                CREATE INDEX IF NOT EXISTS remote_devices_active
                    ON remote_devices(revoked_at, last_seen_at);
                CREATE INDEX IF NOT EXISTS remote_synced_sessions_created
                    ON remote_synced_sessions(created_at, name);
                CREATE INDEX IF NOT EXISTS remote_message_deliveries_updated
                    ON remote_message_deliveries(updated_at DESC);
                CREATE INDEX IF NOT EXISTS remote_approval_audit_resolved
                    ON remote_approval_audit(resolved_at DESC, id);
                INSERT OR IGNORE INTO remote_settings(id, public_base_url)
                    VALUES (1, '');
                """
            )

    def get_public_base_url(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT public_base_url FROM remote_settings WHERE id = 1"
            ).fetchone()
        return str(row["public_base_url"] if row else "")

    def set_public_base_url(self, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE remote_settings SET public_base_url = ? WHERE id = 1",
                (value,),
            )

    def create_pairing(self, *, lifetime_seconds: int = 180) -> dict:
        pairing_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        comparison_code = f"{secrets.randbelow(1_000_000):06d}"
        created_at = utc_now()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM remote_pairings WHERE expires_at < ? OR claimed_at IS NOT NULL",
                (created_at,),
            )
            connection.execute(
                """INSERT INTO remote_pairings(
                       id, secret_hash, comparison_code, expires_at, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (pairing_id, _digest(secret), comparison_code, expires_at, created_at),
            )
        return {
            "id": pairing_id,
            "secret": secret,
            "comparisonCode": comparison_code,
            "expiresAt": expires_at,
        }

    def claim_pairing(self, pairing_id: str, secret: str, device_name: str) -> dict:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pairing = connection.execute(
                "SELECT * FROM remote_pairings WHERE id = ?", (pairing_id,)
            ).fetchone()
            valid = bool(
                pairing
                and pairing["claimed_at"] is None
                and pairing["expires_at"] >= now
                and hmac.compare_digest(pairing["secret_hash"], _digest(secret))
            )
            if not valid:
                raise PairingRejected("pairing is invalid, expired, or already used")
            token = secrets.token_urlsafe(48)
            device_id = str(uuid4())
            safe_name = device_name.strip()[:80] or "移动设备"
            connection.execute(
                """INSERT INTO remote_devices(
                       id, name, token_hash, created_at, last_seen_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (device_id, safe_name, _digest(token), now, now),
            )
            connection.execute(
                "UPDATE remote_pairings SET claimed_at = ? WHERE id = ?",
                (now, pairing_id),
            )
        return {
            "deviceId": device_id,
            "deviceName": safe_name,
            "deviceToken": token,
            "comparisonCode": str(pairing["comparison_code"]),
        }

    def authenticate(self, token: str) -> dict | None:
        if not token:
            return None
        token_hash = _digest(token)
        monotonic_now = time.monotonic()
        with self._cache_lock:
            cached = self._authentication_cache.get(token_hash)
            if cached is not None and cached[0] >= monotonic_now:
                return dict(cached[1])
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, name, created_at, last_seen_at
                   FROM remote_devices
                   WHERE token_hash = ? AND revoked_at IS NULL""",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            with self._cache_lock:
                last_write = self._last_seen_writes.get(row["id"], 0.0)
                should_write = monotonic_now - last_write >= self._last_seen_write_seconds
                if should_write:
                    self._last_seen_writes[row["id"]] = monotonic_now
            if should_write:
                connection.execute(
                    "UPDATE remote_devices SET last_seen_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
        device = {
            "id": row["id"],
            "name": row["name"],
            "createdAt": row["created_at"],
            "lastSeenAt": now if should_write else row["last_seen_at"],
        }
        with self._cache_lock:
            self._authentication_cache[token_hash] = (
                monotonic_now + self._authentication_ttl,
                device,
            )
        return dict(device)

    def list_devices(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, name, created_at, last_seen_at
                   FROM remote_devices WHERE revoked_at IS NULL
                   ORDER BY last_seen_at DESC, created_at DESC"""
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "createdAt": row["created_at"],
                "lastSeenAt": row["last_seen_at"],
            }
            for row in rows
        ]

    def revoke_device(self, device_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE remote_devices SET revoked_at = ?
                   WHERE id = ? AND revoked_at IS NULL""",
                (utc_now(), device_id),
            )
        revoked = cursor.rowcount == 1
        if revoked:
            with self._cache_lock:
                self._authentication_cache = {
                    key: value
                    for key, value in self._authentication_cache.items()
                    if value[1].get("id") != device_id
                }
                self._last_seen_writes.pop(device_id, None)
        return revoked

    @staticmethod
    def _synced_session(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "threadId": row["thread_id"],
            "name": row["name"],
            "createdAt": row["created_at"],
        }

    def create_synced_session(self, *, name: str, thread_id: str) -> dict:
        session_id = str(uuid4())
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO remote_synced_sessions(id, thread_id, name, created_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, thread_id, name, created_at),
            )
            row = connection.execute(
                "SELECT * FROM remote_synced_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        assert row is not None
        result = self._synced_session(row)
        with self._cache_lock:
            self._synced_sessions_cache = None
        return result

    def list_synced_sessions(self) -> list[dict]:
        with self._cache_lock:
            if self._synced_sessions_cache is not None:
                return [dict(item) for item in self._synced_sessions_cache]
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM remote_synced_sessions
                   ORDER BY created_at, name, id"""
            ).fetchall()
        sessions = [self._synced_session(row) for row in rows]
        with self._cache_lock:
            self._synced_sessions_cache = [dict(item) for item in sessions]
        return sessions

    def synced_thread_ids(self) -> set[str]:
        return {session["threadId"] for session in self.list_synced_sessions()}

    def get_synced_session(self, session_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_synced_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._synced_session(row) if row is not None else None

    def delete_synced_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM remote_synced_sessions WHERE id = ?", (session_id,)
            )
        deleted = cursor.rowcount == 1
        if deleted:
            with self._cache_lock:
                self._synced_sessions_cache = None
        return deleted

    def claim_message_delivery(
        self,
        *,
        session_id: str,
        client_message_id: str,
        content_hash: str,
    ) -> dict:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT content_hash, state, result_json, error_message
                   FROM remote_message_deliveries
                   WHERE session_id = ? AND client_message_id = ?""",
                (session_id, client_message_id),
            ).fetchone()
            if row is not None:
                if row["content_hash"] != content_hash:
                    raise MessageDeliveryConflict(
                        "client message ID was reused with different content"
                    )
                result = json.loads(row["result_json"]) if row["result_json"] else None
                return {
                    "claimed": False,
                    "state": row["state"],
                    "result": result,
                    "error": row["error_message"],
                }
            connection.execute(
                """INSERT INTO remote_message_deliveries(
                       session_id, client_message_id, content_hash, state,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, 'pending', ?, ?)""",
                (session_id, client_message_id, content_hash, now, now),
            )
        return {"claimed": True, "state": "pending", "result": None, "error": ""}

    def complete_message_delivery(
        self,
        *,
        session_id: str,
        client_message_id: str,
        result: dict,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE remote_message_deliveries
                   SET state = 'delivered', result_json = ?, error_message = '',
                       updated_at = ?
                   WHERE session_id = ? AND client_message_id = ?""",
                (
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                    session_id,
                    client_message_id,
                ),
            )

    def mark_message_delivery_uncertain(
        self,
        *,
        session_id: str,
        client_message_id: str,
        error_message: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE remote_message_deliveries
                   SET state = 'uncertain', error_message = ?, updated_at = ?
                   WHERE session_id = ? AND client_message_id = ?""",
                (error_message[:500], utc_now(), session_id, client_message_id),
            )

    def release_message_delivery(
        self, *, session_id: str, client_message_id: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """DELETE FROM remote_message_deliveries
                   WHERE session_id = ? AND client_message_id = ?""",
                (session_id, client_message_id),
            )

    def record_approval_audit(self, record: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO remote_approval_audit(
                       id, thread_id, turn_id, method, decision, outcome,
                       actor_device_id, actor_device_name, created_at, resolved_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"],
                    record["threadId"],
                    record.get("turnId") or "",
                    record["method"],
                    record["decision"],
                    record["outcome"],
                    record.get("actorDeviceId") or "",
                    record.get("actorDeviceName") or "",
                    record["createdAt"],
                    record["resolvedAt"],
                ),
            )

    def list_approval_audit(self, limit: int = 50) -> list[dict]:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM remote_approval_audit
                   ORDER BY resolved_at DESC, id DESC LIMIT ?""",
                (bounded,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "threadId": row["thread_id"],
                "turnId": row["turn_id"],
                "method": row["method"],
                "decision": row["decision"],
                "outcome": row["outcome"],
                "actorDeviceId": row["actor_device_id"],
                "actorDeviceName": row["actor_device_name"],
                "createdAt": row["created_at"],
                "resolvedAt": row["resolved_at"],
            }
            for row in rows
        ]
