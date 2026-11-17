import os
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from identity_vault import IdentityVault, VaultUnavailable


class IdentityVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = IdentityVault(self.tmp.name, Fernet.generate_key().decode())

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_creation_default_and_owner_isolation(self):
        first = self.vault.create_profile("owner-a", "Primary")
        second = self.vault.create_profile("owner-a", "Secondary")
        other = self.vault.create_profile("owner-b", "Other")
        self.assertTrue(first["is_default"])
        self.assertFalse(second["is_default"])
        self.assertEqual(len(self.vault.list_profiles("owner-a")), 2)
        self.assertEqual(len(self.vault.list_profiles("owner-b")), 1)
        with self.assertRaises(KeyError):
            self.vault.get_profile("owner-b", first["id"])
        self.assertNotEqual(first["id"], other["id"])

    def test_storage_state_is_encrypted_and_restorable(self):
        profile = self.vault.create_profile("owner", "Google")
        state = {"cookies": [{"name": "session", "value": "not-a-real-secret"}], "origins": []}
        saved = self.vault.save_storage_state("owner", profile["id"], state, account_email="test@example.com")
        self.assertTrue(saved["has_saved_session"])
        self.assertEqual(self.vault.load_storage_state("owner", profile["id"]), state)
        raw = next(Path(self.tmp.name, "states").iterdir()).read_bytes()
        self.assertNotIn(b"not-a-real-secret", raw)
        self.assertEqual(saved["auth_status"], "session_saved")

    def test_disconnect_and_delete_remove_session_data(self):
        profile = self.vault.create_profile("owner", "Google")
        self.vault.save_storage_state("owner", profile["id"], {"cookies": []})
        state_path = next(Path(self.tmp.name, "states").iterdir())
        self.assertTrue(state_path.exists())
        disconnected = self.vault.disconnect("owner", profile["id"])
        self.assertFalse(disconnected["has_saved_session"])
        self.assertFalse(state_path.exists())
        self.vault.delete_profile("owner", profile["id"])
        self.assertEqual(self.vault.list_profiles("owner"), [])

    def test_missing_key_disables_vault(self):
        with self.assertRaises(VaultUnavailable):
            IdentityVault(self.tmp.name, "")
