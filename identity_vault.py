"""Encrypted, file-backed browser identity vault.

The vault never stores passwords. It stores only profile metadata and encrypted
Playwright storage_state snapshots. Production deployments must provide
IDENTITY_VAULT_KEY and a persistent IDENTITY_VAULT_DIR (for example a Render
persistent disk or external mounted volume).
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cryptography.fernet import Fernet, InvalidToken


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    if not value or len(value) > 80 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
        raise ValueError("Invalid profile identifier")
    return value


class VaultUnavailable(RuntimeError):
    pass


class IdentityVault:
    """SQLite metadata plus Fernet-encrypted state snapshots."""

    def __init__(self, root: str | Path | None = None, key: str | None = None) -> None:
        self.root = Path(root or os.getenv("IDENTITY_VAULT_DIR", "/var/data/identity-vault"))
        raw_key = key or os.getenv("IDENTITY_VAULT_KEY", "").strip()
        if not raw_key:
            raise VaultUnavailable("Identity Vault is not configured.")
        try:
            self._fernet = Fernet(raw_key.encode("ascii"))
        except Exception as exc:
            raise VaultUnavailable("IDENTITY_VAULT_KEY is invalid.") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        self._db_path = self.root / "vault.sqlite3"
        self._init_db()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    account_email TEXT,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    auto_reuse INTEGER NOT NULL DEFAULT 1,
                    auth_status TEXT NOT NULL DEFAULT 'not_connected',
                    last_successful_auth TEXT,
                    last_activity TEXT,
                    session_expires_at TEXT,
                    state_path TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS profiles_owner_idx ON profiles(owner_id);
                """
            )

    def _profile_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "account_email": row["account_email"],
            "is_default": bool(row["is_default"]),
            "auto_reuse": bool(row["auto_reuse"]),
            "auth_status": row["auth_status"],
            "last_successful_auth": row["last_successful_auth"],
            "last_activity": row["last_activity"],
            "session_expires_at": row["session_expires_at"],
            "has_saved_session": bool(row["state_path"]),
            "created_at": row["created_at"],
        }

    def create_profile(self, owner_id: str, name: str, auto_reuse: bool = True) -> dict[str, Any]:
        owner_id = _safe_id(owner_id)
        name = name.strip()[:100]
        if not name:
            raise ValueError("Profile name is required")
        profile_id = secrets.token_urlsafe(18).replace("_", "-")
        now = utc_now()
        with self._db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM profiles WHERE owner_id = ?", (owner_id,)).fetchone()[0]
            is_default = int(count == 0)
            conn.execute(
                "INSERT INTO profiles(id,owner_id,name,is_default,auto_reuse,created_at) VALUES(?,?,?,?,?,?)",
                (profile_id, owner_id, name, is_default, int(auto_reuse), now),
            )
        return self.get_profile(owner_id, profile_id)

    def list_profiles(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = _safe_id(owner_id)
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM profiles WHERE owner_id = ? ORDER BY created_at", (owner_id,)).fetchall()
        return [self._profile_dict(row) for row in rows]

    def _row(self, owner_id: str, profile_id: str) -> sqlite3.Row:
        owner_id = _safe_id(owner_id)
        profile_id = _safe_id(profile_id)
        with self._db() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE owner_id = ? AND id = ?", (owner_id, profile_id)).fetchone()
        if row is None:
            raise KeyError("Profile not found")
        return row

    def get_profile(self, owner_id: str, profile_id: str) -> dict[str, Any]:
        return self._profile_dict(self._row(owner_id, profile_id))

    def update_profile(self, owner_id: str, profile_id: str, *, name: str | None = None, auto_reuse: bool | None = None) -> dict[str, Any]:
        self._row(owner_id, profile_id)
        updates: list[str] = []
        values: list[Any] = []
        if name is not None:
            name = name.strip()[:100]
            if not name:
                raise ValueError("Profile name is required")
            updates.append("name = ?"); values.append(name)
        if auto_reuse is not None:
            updates.append("auto_reuse = ?"); values.append(int(auto_reuse))
        if updates:
            values.extend([owner_id, profile_id])
            with self._db() as conn:
                conn.execute(f"UPDATE profiles SET {', '.join(updates)} WHERE owner_id = ? AND id = ?", values)
        return self.get_profile(owner_id, profile_id)

    def set_default(self, owner_id: str, profile_id: str) -> dict[str, Any]:
        self._row(owner_id, profile_id)
        with self._db() as conn:
            conn.execute("UPDATE profiles SET is_default = 0 WHERE owner_id = ?", (owner_id,))
            conn.execute("UPDATE profiles SET is_default = 1 WHERE owner_id = ? AND id = ?", (owner_id, profile_id))
        return self.get_profile(owner_id, profile_id)

    def delete_profile(self, owner_id: str, profile_id: str) -> None:
        row = self._row(owner_id, profile_id)
        with self._db() as conn:
            conn.execute("DELETE FROM profiles WHERE owner_id = ? AND id = ?", (owner_id, profile_id))
        if row["state_path"]:
            Path(row["state_path"]).unlink(missing_ok=True)

    def disconnect(self, owner_id: str, profile_id: str) -> dict[str, Any]:
        row = self._row(owner_id, profile_id)
        if row["state_path"]:
            Path(row["state_path"]).unlink(missing_ok=True)
        with self._db() as conn:
            conn.execute(
                "UPDATE profiles SET account_email=NULL, auth_status='not_connected', last_successful_auth=NULL, session_expires_at=NULL, state_path=NULL WHERE owner_id=? AND id=?",
                (owner_id, profile_id),
            )
        return self.get_profile(owner_id, profile_id)

    def save_storage_state(self, owner_id: str, profile_id: str, state: dict[str, Any], *, account_email: str | None = None) -> dict[str, Any]:
        row = self._row(owner_id, profile_id)
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        token = self._fernet.encrypt(payload)
        state_dir = self.root / "states"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        path = state_dir / f"{row['id']}.bin"
        path.write_bytes(token)
        os.chmod(path, 0o600)
        now = utc_now()
        with self._db() as conn:
            conn.execute(
                "UPDATE profiles SET account_email=?, auth_status='session_saved', last_successful_auth=NULL, last_activity=?, state_path=? WHERE owner_id=? AND id=?",
                (account_email, now, str(path), owner_id, profile_id),
            )
        return self.get_profile(owner_id, profile_id)

    def load_storage_state(self, owner_id: str, profile_id: str) -> dict[str, Any] | None:
        row = self._row(owner_id, profile_id)
        path = row["state_path"]
        if not path:
            return None
        try:
            raw = self._fernet.decrypt(Path(path).read_bytes())
            return json.loads(raw.decode("utf-8"))
        except (OSError, InvalidToken, json.JSONDecodeError) as exc:
            raise VaultUnavailable("Saved browser session could not be restored safely.") from exc

    def touch_activity(self, owner_id: str, profile_id: str) -> None:
        self._row(owner_id, profile_id)
        with self._db() as conn:
            conn.execute("UPDATE profiles SET last_activity=? WHERE owner_id=? AND id=?", (utc_now(), owner_id, profile_id))

    @staticmethod
    def verify_admin_key(provided: str | None) -> bool:
        expected = os.getenv("PROFILE_ADMIN_KEY", "").strip()
        return bool(expected and provided and hmac.compare_digest(provided, expected))

    @staticmethod
    def owner_id_from_header(value: str | None) -> str:
        owner = (value or "").strip()
        if not owner or len(owner) > 80:
            raise ValueError("X-Profile-Owner header is required")
        return _safe_id(owner)
