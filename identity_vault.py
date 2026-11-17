"""Encrypted, fail-safe browser identity vault.

The vault stores profile metadata in SQLite and Playwright storage_state snapshots
as Fernet-encrypted files. In production, IDENTITY_VAULT_DIR must be backed by a
real persistent mount and REQUIRE_PERSISTENT_STORAGE must be true. There is no
ephemeral-storage fallback.
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


SESSION_STATUSES = {
    "session_created",
    "session_saved",
    "session_restored",
    "session_expired",
    "session_invalid",
    "session_recovery_failed",
    "not_connected",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str) -> str:
    if not value or len(value) > 80 or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for ch in value
    ):
        raise ValueError("Invalid profile identifier")
    return value


class VaultUnavailable(RuntimeError):
    pass


class IdentityVault:
    """SQLite metadata plus Fernet-encrypted state snapshots."""

    def __init__(self, root: str | Path | None = None, key: str | None = None) -> None:
        configured_root = root or os.getenv("IDENTITY_VAULT_DIR", "/var/data/identity-vault")
        self.root = Path(configured_root).expanduser()
        raw_key = key or os.getenv("IDENTITY_VAULT_KEY", "").strip()
        if not raw_key:
            raise VaultUnavailable("Identity Vault is not configured.")
        try:
            self._fernet = Fernet(raw_key.encode("ascii"))
        except Exception as exc:
            raise VaultUnavailable("IDENTITY_VAULT_KEY is invalid.") from exc
        self._require_persistent = os.getenv("REQUIRE_PERSISTENT_STORAGE", "false").lower() == "true"
        self._prepare_storage()
        self._db_path = self.root / "vault.sqlite3"
        self._init_db()

    def _prepare_storage(self) -> None:
        if self.root.is_absolute() is False:
            raise VaultUnavailable("IDENTITY_VAULT_DIR must be an absolute path.")
        if self._require_persistent:
            mount = Path(os.getenv("PERSISTENT_STORAGE_MOUNT", "/var/data")).expanduser()
            if not mount.is_absolute() or not mount.exists() or not os.path.ismount(mount):
                raise VaultUnavailable("Persistent Identity Vault storage is not mounted.")
            try:
                self.root.resolve().relative_to(mount.resolve())
            except ValueError as exc:
                raise VaultUnavailable("Identity Vault path is outside the persistent mount.") from exc
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not os.access(self.root, os.W_OK | os.X_OK):
                raise OSError("storage directory is not writable")
            marker = self.root / ".identity-vault-marker"
            if not marker.exists():
                marker.write_text("identity-vault\n", encoding="ascii")
                os.chmod(marker, 0o600)
                with marker.open("rb") as handle:
                    os.fsync(handle.fileno())
        except OSError as exc:
            raise VaultUnavailable("Identity Vault storage is unavailable or not writable.") from exc

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
                    auth_status TEXT NOT NULL DEFAULT 'session_created',
                    last_successful_auth TEXT,
                    last_activity TEXT,
                    session_expires_at TEXT,
                    state_path TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS profiles_owner_idx ON profiles(owner_id);
                """
            )

    def _set_status(self, owner_id: str, profile_id: str, status: str) -> None:
        if status not in SESSION_STATUSES:
            raise ValueError("Invalid session status")
        with self._db() as conn:
            conn.execute(
                "UPDATE profiles SET auth_status=?, last_activity=? WHERE owner_id=? AND id=?",
                (status, utc_now(), owner_id, profile_id),
            )

    def _profile_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        state_path = Path(row["state_path"]) if row["state_path"] else None
        state_present = bool(state_path and state_path.is_file())
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
            "has_saved_session": state_present,
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
                "INSERT INTO profiles(id,owner_id,name,is_default,auto_reuse,auth_status,created_at) VALUES(?,?,?,?,?,?,?)",
                (profile_id, owner_id, name, is_default, int(auto_reuse), "session_created", now),
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
            updates.append("name = ?")
            values.append(name)
        if auto_reuse is not None:
            updates.append("auto_reuse = ?")
            values.append(int(auto_reuse))
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
                "UPDATE profiles SET account_email=NULL, auth_status='session_invalid', last_successful_auth=NULL, session_expires_at=NULL, state_path=NULL WHERE owner_id=? AND id=?",
                (owner_id, profile_id),
            )
        return self.get_profile(owner_id, profile_id)

    def mark_expired(self, owner_id: str, profile_id: str) -> dict[str, Any]:
        self._row(owner_id, profile_id)
        self._set_status(owner_id, profile_id, "session_expired")
        return self.get_profile(owner_id, profile_id)

    def save_storage_state(self, owner_id: str, profile_id: str, state: dict[str, Any], *, account_email: str | None = None) -> dict[str, Any]:
        row = self._row(owner_id, profile_id)
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        token = self._fernet.encrypt(payload)
        state_dir = self.root / "states"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        path = state_dir / f"{row['id']}.bin"
        tmp = state_dir / f".{row['id']}.tmp"
        tmp.write_bytes(token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        now = utc_now()
        with self._db() as conn:
            conn.execute(
                "UPDATE profiles SET account_email=?, auth_status='session_saved', last_successful_auth=NULL, last_activity=?, state_path=? WHERE owner_id=? AND id=?",
                (account_email, now, str(path), owner_id, profile_id),
            )
        return self.get_profile(owner_id, profile_id)

    def load_storage_state(self, owner_id: str, profile_id: str) -> dict[str, Any] | None:
        row = self._row(owner_id, profile_id)
        path_value = row["state_path"]
        if not path_value:
            self._set_status(owner_id, profile_id, "session_invalid")
            return None
        path = Path(path_value)
        try:
            path.resolve().relative_to(self.root.resolve())
            raw = self._fernet.decrypt(path.read_bytes())
            state = json.loads(raw.decode("utf-8"))
            if not isinstance(state, dict):
                raise ValueError("storage state must be an object")
        except (OSError, InvalidToken, json.JSONDecodeError, ValueError) as exc:
            self._set_status(owner_id, profile_id, "session_recovery_failed")
            raise VaultUnavailable("Saved browser session could not be restored safely.") from exc
        self._set_status(owner_id, profile_id, "session_restored")
        return state

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
