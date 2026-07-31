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
