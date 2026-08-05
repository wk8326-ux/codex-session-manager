from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from remote.store import PairingRejected, RemoteStore


class RemoteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "watchdog.db"
        self.store = RemoteStore(self.database)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_pairing_is_single_use_and_secrets_are_only_stored_as_hashes(self) -> None:
        pairing = self.store.create_pairing()
        claimed = self.store.claim_pairing(
            pairing["id"], pairing["secret"], "My phone"
        )

        connection = sqlite3.connect(self.database)
        pairing_hash = connection.execute(
            "SELECT secret_hash FROM remote_pairings WHERE id = ?", (pairing["id"],)
        ).fetchone()[0]
        token_hash = connection.execute(
            "SELECT token_hash FROM remote_devices WHERE id = ?", (claimed["deviceId"],)
        ).fetchone()[0]
        connection.close()

        self.assertNotEqual(pairing_hash, pairing["secret"])
        self.assertNotEqual(token_hash, claimed["deviceToken"])
        self.assertIsNotNone(self.store.authenticate(claimed["deviceToken"]))
        reopened_store = RemoteStore(self.database)
        reopened_store.initialize()
        self.assertEqual(
            reopened_store.authenticate(claimed["deviceToken"])["name"], "My phone"
        )
        with self.assertRaises(PairingRejected):
            self.store.claim_pairing(pairing["id"], pairing["secret"], "Other")

    def test_expired_pairing_is_rejected(self) -> None:
        pairing = self.store.create_pairing()
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE remote_pairings SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (pairing["id"],),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(PairingRejected):
            self.store.claim_pairing(pairing["id"], pairing["secret"], "Phone")

    def test_revocation_invalidates_only_the_selected_device(self) -> None:
        devices = []
        for name in ("Phone", "Tablet"):
            pairing = self.store.create_pairing()
            devices.append(self.store.claim_pairing(pairing["id"], pairing["secret"], name))

        self.assertTrue(self.store.revoke_device(devices[0]["deviceId"]))
        self.assertIsNone(self.store.authenticate(devices[0]["deviceToken"]))
        self.assertIsNotNone(self.store.authenticate(devices[1]["deviceToken"]))

    def test_synced_sessions_are_persisted_independently_and_thread_ids_are_unique(self) -> None:
        created = self.store.create_synced_session(
            name="Woxsheet",
            thread_id="00000000-0000-4000-8000-000000000001",
        )

        self.assertEqual(self.store.list_synced_sessions(), [created])
        self.assertEqual(self.store.get_synced_session(created["id"]), created)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_synced_session(
                name="Duplicate",
                thread_id="00000000-0000-4000-8000-000000000001",
            )

        self.assertTrue(self.store.delete_synced_session(created["id"]))
        self.assertEqual(self.store.list_synced_sessions(), [])


if __name__ == "__main__":
    unittest.main()
