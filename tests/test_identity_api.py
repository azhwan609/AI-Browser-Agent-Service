import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import main


class IdentityApiTests(unittest.TestCase):
    def test_profile_api_is_authorized_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {
                "IDENTITY_VAULT_DIR": root,
                "IDENTITY_VAULT_KEY": Fernet.generate_key().decode(),
                "PROFILE_ADMIN_KEY": "admin-test-key",
            },
            clear=False,
        ):
            client = TestClient(main.app)
            owner_a = {"X-Profile-Admin-Key": "admin-test-key", "X-Profile-Owner": "owner-a"}
            owner_b = {"X-Profile-Admin-Key": "admin-test-key", "X-Profile-Owner": "owner-b"}
            self.assertEqual(client.get("/api/profiles", headers={"X-Profile-Owner": "owner-a"}).status_code, 401)
            created = client.post("/api/profiles", headers=owner_a, json={"name": "Primary"})
            self.assertEqual(created.status_code, 200)
            profile_id = created.json()["id"]
            self.assertEqual(client.get("/api/profiles", headers=owner_b).json()["profiles"], [])
            self.assertEqual(client.get(f"/api/profiles/{profile_id}", headers=owner_b).status_code, 404)

            blocked = client.post(
                f"/api/profiles/{profile_id}/auth-status",
                headers=owner_a,
                json={"status": "active", "human_verified": False},
            )
            self.assertEqual(blocked.status_code, 409)

            deleted = client.delete(f"/api/profiles/{profile_id}", headers=owner_a)
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(client.get("/api/profiles", headers=owner_a).json()["profiles"], [])

    def test_vault_disabled_without_config(self):
        with patch.dict(os.environ, {"IDENTITY_VAULT_KEY": "", "PROFILE_ADMIN_KEY": "admin-test-key"}, clear=False):
            client = TestClient(main.app)
            response = client.get(
                "/api/profiles",
                headers={"X-Profile-Admin-Key": "admin-test-key", "X-Profile-Owner": "owner-a"},
            )
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
