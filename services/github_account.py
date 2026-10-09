"""Bounded GitHub device sign-in; credentials never cross the API boundary."""
import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

# Public client ID of the "Kairos" GitHub OAuth App (device flow, no secret).
CLIENT_ID = "Ov23liYYhbpQVkY1UurQ"
FILE = Path(DATA_DIR) / "github-account.json"
DEVICE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
_pending = None
_lock = asyncio.Lock()


def _now():
    return time.monotonic()


class GitHubError(ValueError):
    def __init__(self, status):
        self.status = status
        message = {401: "GitHub authorization expired. Sign in again.",
                   403: "GitHub refused this request. Check public_repo permission or try again after the rate limit resets.",
                   404: "GitHub repository or branch is not available yet.",
                   422: "GitHub could not create this branch or pull request. It may already exist."}.get(status, "GitHub request failed. Try again later.")
        super().__init__(message)


def client():
    """One mockable factory for all GitHub HTTP."""
    return httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)


async def request(method, url, *, token=None, data=None, body=None, params=None):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc not in ("github.com", "api.github.com")
            or parsed.query or parsed.fragment or (parsed.netloc == "github.com" and url not in (DEVICE_URL, TOKEN_URL))
            or (token and parsed.netloc != "api.github.com")):
        raise ValueError("GitHub request requires an allowlisted HTTPS endpoint")
    headers = {"Accept": "application/json" if parsed.netloc == "github.com" else "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with client() as connection:
        async with connection.stream(method, url, headers=headers, data=data, json=body, params=params) as response:
            if not 200 <= response.status_code < 300:
                raise GitHubError(response.status_code)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 2 * 1024 * 1024:
                    raise ValueError("GitHub response exceeds size limit")
    return json.loads(content)


def _client_id():
    return os.environ.get("KAIROS_GITHUB_CLIENT_ID", CLIENT_ID).strip()


def credentials():
    saved = read_json(str(FILE), {})
    if not saved.get("account"):
        raise ValueError("Sign in with GitHub in the Tool Store first")
    return json.loads(decrypt(saved["account"]))


def status():
    saved = read_json(str(FILE), {})
    account = json.loads(decrypt(saved["account"])) if saved.get("account") else {}
    return {"configured": bool(_client_id()), "signed_in": bool(account.get("token")), "login": account.get("login")}


async def start():
    global _pending
    async with _lock:
        ident = _client_id()
        if not ident:
            raise ValueError("GitHub sign-in isn't set up in this build yet")
        result = await request("POST", DEVICE_URL, data={"client_id": ident, "scope": "public_repo"})
        if result.get("verification_uri") != "https://github.com/login/device":
            raise ValueError("GitHub returned an unexpected verification URI")
        expires, interval = int(result["expires_in"]), max(1, int(result.get("interval", 5)))
        _pending = {"device_code": result["device_code"], "client_id": ident,
                    "expires": _now() + expires, "next": _now() + interval, "interval": interval}
        return {"user_code": result["user_code"], "verification_uri": result["verification_uri"], "expires_in": expires, "interval": interval}


async def poll():
    global _pending
    async with _lock:
        if not _pending or _now() >= _pending["expires"]:
            _pending = None
            raise ValueError("GitHub sign-in expired. Start again.")
        if _now() < _pending["next"]:
            return {"state": "authorization_pending", "interval": _pending["interval"]}
        result = await request("POST", TOKEN_URL, data={"client_id": _pending["client_id"], "device_code": _pending["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        error = result.get("error")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                _pending["interval"] += 5
            _pending["next"] = _now() + _pending["interval"]
            return {"state": error, "interval": _pending["interval"]}
        if error:
            _pending = None
            raise ValueError("GitHub sign-in expired. Start again." if error == "expired_token" else "GitHub sign-in was denied. Start again.")
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub did not return an access token")
        profile = await request("GET", "https://api.github.com/user", token=token)
        login = profile.get("login")
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", login) or len(login) > 39:
            raise ValueError("GitHub returned an invalid login")
        write_json_atomic(str(FILE), {"account": encrypt(json.dumps({"login": login, "token": token}))})
        _pending = None
        return {**status(), "state": "signed_in"}


async def sign_out():
    global _pending
    async with _lock:
        _pending = None
        FILE.unlink(missing_ok=True)
        return status()


async def api(method, path, *, body=None, params=None, expected_login=None):
    account = credentials()
    if expected_login is not None and account["login"] != expected_login:
        raise ValueError("The GitHub account changed. Preview and publish again.")
    # Tokens are used only for this store, never arbitrary repositories.
    if not (path == "/repos/david-darr/kairos-store" or path.startswith("/repos/david-darr/kairos-store/")
            or path == f"/repos/{account['login']}/kairos-store" or path.startswith(f"/repos/{account['login']}/kairos-store/")) or any(s in path for s in ("..", "%", "?", "\\")):
        raise ValueError("GitHub token use is restricted to kairos-store")
    return await request(method, "https://api.github.com" + path, token=account["token"], body=body, params=params)
