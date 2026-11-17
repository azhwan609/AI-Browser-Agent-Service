import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from cryptography.fernet import Fernet

from identity_vault import IdentityVault, VaultUnavailable


class IdentityVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.vault = IdentityVault(self.tmp.name, self.key)

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_creation_default_and_owner_isolation(self):
        first = self.vault.create_profile("owner-a", "Primary")
        second = self.vault.create_profile("owner-a", "Secondary")
        other = self.vault.create_profile("owner-b", "Other")
        self.assertTrue(first["is_default"])
        self.assertEqual(first["auth_status"], "session_created")
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
        self.assertEqual(self.vault.get_profile("owner", profile["id"])["auth_status"], "session_restored")
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
        self.assertEqual(disconnected["auth_status"], "session_invalid")
        self.assertFalse(state_path.exists())
        self.vault.delete_profile("owner", profile["id"])
        self.assertEqual(self.vault.list_profiles("owner"), [])

    def test_restart_reloads_encrypted_state_and_preserves_owner_profile(self):
        profile = self.vault.create_profile("owner", "Persistent")
        state = {"cookies": [], "origins": [{"origin": "https://example.com", "localStorage": []}]}
        self.vault.save_storage_state("owner", profile["id"], state)
        restarted = IdentityVault(self.tmp.name, self.key)
        restored = restarted.load_storage_state("owner", profile["id"])
        self.assertEqual(restored, state)
        current = restarted.get_profile("owner", profile["id"])
        self.assertEqual(current["name"], "Persistent")
        self.assertEqual(current["auth_status"], "session_restored")
        self.assertEqual(restarted.list_profiles("other"), [])


    def test_required_persistent_mount_never_falls_back_to_ephemeral_directory(self):
        with patch.dict(
            os.environ,
            {"REQUIRE_PERSISTENT_STORAGE": "true", "PERSISTENT_STORAGE_MOUNT": self.tmp.name},
            clear=False,
        ), patch("identity_vault.os.path.ismount", return_value=False):
            with self.assertRaises(VaultUnavailable):
                IdentityVault(self.tmp.name, self.key)

    def test_corrupt_or_missing_state_gets_recovery_failure_status(self):
        profile = self.vault.create_profile("owner", "Recovery")
        self.vault.save_storage_state("owner", profile["id"], {"cookies": []})
        state_path = next(Path(self.tmp.name, "states").iterdir())
        state_path.write_bytes(b"corrupted")
        with self.assertRaises(VaultUnavailable):
            self.vault.load_storage_state("owner", profile["id"])
        self.assertEqual(self.vault.get_profile("owner", profile["id"])["auth_status"], "session_recovery_failed")


    def test_missing_key_disables_vault(self):
        with self.assertRaises(VaultUnavailable):
            IdentityVault(self.tmp.name, "")
