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


if __name__ == "__main__":
    unittest.main()

