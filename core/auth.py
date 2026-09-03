"""Auth: single-user-by-default with a real login/session model when enabled,
matching Odysseus's own approach (see JARVIS Plan's v2 scoping — "match what
Odysseus uses" for auth was David's explicit call, 2026-08-31).

- AUTH_ENABLED=false (default): single trusted local user, no login screen —
  the sane default for the Electron desktop app running on one person's
  machine. Every request resolves to the one local user.
- AUTH_ENABLED=true: real accounts. bcrypt password hashes, session cookies,
  optional per-user TOTP 2FA, admin/non-admin privilege split. This is the
  mode the plain-web access path ("front door #2") should run in for anyone
  exposing the backend beyond localhost.

Reserved usernames ("internal-tool", "api") can never be registered — see
INTERNAL_TOOL_TOKEN below, which impersonates "internal-tool" for the app's
own agent/tool calls so they don't need a browser session.
"""
import os
import secrets
import time
from typing import Optional

import bcrypt
import pyotp

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR

AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

SESSION_COOKIE_NAME = "jarvis_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days, matches Odysseus

RESERVED_USERNAMES = {"internal-tool", "api"}

SINGLE_USER = "local"

# Generated once per process, never persisted. The app's own tool-call
# machinery presents this via a header to satisfy require_admin() without a
# browser session, mirroring Odysseus's internal-tool loopback exactly.
INTERNAL_TOOL_TOKEN = secrets.token_hex(32)


def auth_enabled() -> bool:
    return os.getenv("AUTH_ENABLED", "false").lower() in {"1", "true", "yes"}


class AuthManager:
    def __init__(self) -> None:
        self._users: dict = read_json(AUTH_FILE, {"users": {}})
        self._sessions: dict = read_json(SESSIONS_FILE, {})

    # -- users --------------------------------------------------------

    def has_any_users(self) -> bool:
        return bool(self._users.get("users"))

    def create_user(self, username: str, password: str, is_admin: bool = False) -> None:
        if username in RESERVED_USERNAMES:
            raise ValueError(f"'{username}' is a reserved username")
        if username in self._users["users"]:
            raise ValueError(f"user '{username}' already exists")
        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        self._users["users"][username] = {
            "password_hash": password_hash,
            "is_admin": is_admin,
            "totp_secret": None,
            "totp_enabled": False,
            "created_at": time.time(),
        }
        write_json_atomic(AUTH_FILE, self._users)

    def verify_password(self, username: str, password: str) -> bool:
        user = self._users["users"].get(username)
        if not user:
            return False
        return bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8"))

    def is_admin(self, username: str) -> bool:
        if username in ("internal-tool", SINGLE_USER):
            return True
        user = self._users["users"].get(username)
        return bool(user and user.get("is_admin"))

    # -- account/user management (Settings tab, David's ask 2026-08-31,
    # matching Odysseus's Account + Admin > Users panels) -----------------

    def list_users(self) -> list[dict]:
        return [
            {"username": u, "is_admin": info.get("is_admin", False), "totp_enabled": info.get("totp_enabled", False), "created_at": info.get("created_at")}
            for u, info in self._users["users"].items()
        ]

    def change_password(self, username: str, new_password: str) -> None:
        user = self._users["users"].get(username)
        if not user:
            raise KeyError(f"no such user: {username}")
        user["password_hash"] = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        write_json_atomic(AUTH_FILE, self._users)

    def set_admin(self, username: str, is_admin: bool) -> None:
        user = self._users["users"].get(username)
        if not user:
            raise KeyError(f"no such user: {username}")
        if not is_admin and self._count_admins() <= 1 and user.get("is_admin"):
            raise ValueError("can't demote the last admin")
        user["is_admin"] = is_admin
        write_json_atomic(AUTH_FILE, self._users)

    def _count_admins(self) -> int:
        return sum(1 for u in self._users["users"].values() if u.get("is_admin"))

    def delete_user(self, username: str) -> None:
        user = self._users["users"].get(username)
        if not user:
            raise KeyError(f"no such user: {username}")
        if user.get("is_admin") and self._count_admins() <= 1:
            raise ValueError("can't delete the last admin")
        del self._users["users"][username]
        write_json_atomic(AUTH_FILE, self._users)
        # Any live sessions for this user stop authenticating on next use
        # anyway (validate_session re-checks the user exists), but drop them
        # now so /api/auth/users reflects reality immediately.
        stale = [t for t, s in self._sessions.items() if s.get("username") == username]
        for t in stale:
            del self._sessions[t]
        if stale:
            write_json_atomic(SESSIONS_FILE, self._sessions)

    def disable_totp(self, username: str) -> None:
        user = self._users["users"].get(username)
        if not user:
            raise KeyError(f"no such user: {username}")
        user["totp_secret"] = None
        user["totp_enabled"] = False
        write_json_atomic(AUTH_FILE, self._users)

    # -- TOTP (optional 2FA) -------------------------------------------

    def totp_enabled(self, username: str) -> bool:
        user = self._users["users"].get(username)
        return bool(user and user.get("totp_enabled"))

    def start_totp_enrollment(self, username: str) -> str:
        """Generates a new secret (not yet active) and returns its otpauth:// URI."""
        secret = pyotp.random_base32()
        self._users["users"][username]["totp_secret"] = secret
        write_json_atomic(AUTH_FILE, self._users)
        return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name="JARVIS")

    def confirm_totp_enrollment(self, username: str, code: str) -> bool:
        user = self._users["users"].get(username)
        if not user or not user.get("totp_secret"):
            return False
        if not pyotp.totp.TOTP(user["totp_secret"]).verify(code):
            return False
        user["totp_enabled"] = True
        write_json_atomic(AUTH_FILE, self._users)
        return True

    def verify_totp(self, username: str, code: str) -> bool:
        user = self._users["users"].get(username)
        if not user or not user.get("totp_enabled"):
            return False
        return pyotp.totp.TOTP(user["totp_secret"]).verify(code)

    # -- sessions --------------------------------------------------------

    def create_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {"username": username, "expires_at": time.time() + SESSION_TTL_SECONDS}
        write_json_atomic(SESSIONS_FILE, self._sessions)
        return token

    def validate_session(self, token: str) -> Optional[str]:
        session = self._sessions.get(token)
        if not session:
            return None
        if session["expires_at"] < time.time():
            self.delete_session(token)
            return None
        # Re-check the user still exists — a deleted account's cookie stops
        # authenticating on its next use rather than continuing to work.
        if session["username"] not in self._users["users"]:
            return None
        return session["username"]

    def delete_session(self, token: str) -> None:
        if token in self._sessions:
            del self._sessions[token]
            write_json_atomic(SESSIONS_FILE, self._sessions)


auth_manager = AuthManager()
