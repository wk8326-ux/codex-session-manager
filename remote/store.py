from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4


class RemoteStoreError(RuntimeError):
    """Base error for remote access persistence failures."""


class PairingRejected(RemoteStoreError):
    """A pairing secret is invalid, expired, or already used."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RemoteStore:
    def __init__(self, database_path: Path) -> None:
        self._path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
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
                CREATE INDEX IF NOT EXISTS remote_pairings_expiry
                    ON remote_pairings(expires_at, claimed_at);
                CREATE INDEX IF NOT EXISTS remote_devices_active
                    ON remote_devices(revoked_at, last_seen_at);
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
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, name, created_at, last_seen_at
                   FROM remote_devices
                   WHERE token_hash = ? AND revoked_at IS NULL""",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE remote_devices SET last_seen_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        return {
            "id": row["id"],
            "name": row["name"],
            "createdAt": row["created_at"],
            "lastSeenAt": now,
        }

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
        return cursor.rowcount == 1

