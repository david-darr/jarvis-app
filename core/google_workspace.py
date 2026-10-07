"""Google Workspace account connection and bounded Drive, Sheets, Forms API client.

Each installation supplies its own Desktop OAuth client ID. Tokens stay in the
existing encrypted secret store. Only the fixed Google API hosts below are
ever contacted with those tokens; callers cannot provide an arbitrary URL.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import quote, urlencode

import httpx

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt


FILE = os.path.join(DATA_DIR, "google_workspace.json")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
HOSTS = {
    "drive": "https://www.googleapis.com/drive/v3",
    "upload": "https://www.googleapis.com/upload/drive/v3",
    "sheets": "https://sheets.googleapis.com/v4",
    "forms": "https://forms.googleapis.com/v1",
}
SCOPES = (
    "openid", "email",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
)
FILE_FIELDS = "nextPageToken,incompleteSearch,files(id,name,mimeType,size,parents,modifiedTime,webViewLink,description,starred,trashed,driveId,capabilities,owners,shared)"
MAX_DOWNLOAD = 25 * 1024 * 1024
_pending: dict[str, tuple[str, float]] = {}
_refresh_lock = asyncio.Lock()


class GoogleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _load() -> dict:
    return read_json(FILE, {})


def _save(data: dict) -> None:
    write_json_atomic(FILE, data)


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise GoogleError("Invalid Google file ID")
    return value


def status() -> dict:
    data = _load()
    return {"configured": bool(data.get("client_id")),
            "connected": bool(data.get("refresh_token")),
            "email": data.get("email"),
            "client_id": data.get("client_id") or ""}


def configure(client_id: str, client_secret: str = "") -> dict:
    client_id = client_id.strip()
    if len(client_id) > 256 or not client_id.endswith(".apps.googleusercontent.com"):
        raise GoogleError("Enter a Google OAuth Desktop client ID")
    data = _load()
    if data.get("client_id") != client_id:
        data.pop("refresh_token", None)
        data.pop("access_token", None)
        data.pop("email", None)
        data.pop("client_secret", None)
    data["client_id"] = client_id
    if client_secret:
        data["client_secret"] = encrypt(client_secret.strip())
    _save(data)
    return status()


def disconnect() -> dict:
    data = _load()
    for key in ("refresh_token", "access_token", "expires_at", "email"):
        data.pop(key, None)
    _save(data)
    return status()


def start_sign_in(redirect_uri: str) -> str:
    client_id = _load().get("client_id")
    if not client_id:
        raise GoogleError("Set a Google OAuth Desktop client ID first")
    if not re.fullmatch(r"http://127\.0\.0\.1:\d+/api/google/oauth/callback", redirect_uri):
        raise GoogleError("Google sign-in must start on this computer")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    _pending[state] = (verifier, time.time() + 600)
    for stale, (_, expires) in list(_pending.items()):
        if expires < time.time():
            _pending.pop(stale, None)
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return AUTH_URL + "?" + urlencode(params)


async def finish_sign_in(state: str, code: str | None, redirect_uri: str, error: str | None = None) -> str:
    pending = _pending.pop(state, None)
    if not pending or pending[1] < time.time():
        raise GoogleError("This sign-in request expired or was not started here")
    if error or not code:
        raise GoogleError("Google sign-in was cancelled or denied")
    data = _load()
    body = {"client_id": data["client_id"], "code": code, "code_verifier": pending[0],
            "redirect_uri": redirect_uri, "grant_type": "authorization_code"}
    if data.get("client_secret"):
        body["client_secret"] = decrypt(data["client_secret"])
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(TOKEN_URL, data=body)
        if response.status_code >= 400:
            raise GoogleError("Google rejected the sign-in code", response.status_code)
        tokens = response.json()
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise GoogleError("Google did not return an offline access token; reconnect and approve offline access")
        access = tokens.get("access_token")
        email = None
        if access:
            profile = await client.get("https://openidconnect.googleapis.com/v1/userinfo",
                                       headers={"Authorization": f"Bearer {access}"})
            if profile.is_success:
                email = profile.json().get("email")
    data["refresh_token"] = encrypt(refresh)
    data["access_token"] = encrypt(access) if access else None
    data["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
    data["email"] = email
    _save(data)
    return email or "Google account"


async def _token(force: bool = False) -> str:
    async with _refresh_lock:
        data = _load()
        if not data.get("refresh_token"):
            raise GoogleError("Connect Google Workspace in Library first", 401)
        if not force and data.get("access_token") and (data.get("expires_at") or 0) > time.time() + 90:
            return decrypt(data["access_token"])
        body = {"client_id": data["client_id"], "refresh_token": decrypt(data["refresh_token"]),
                "grant_type": "refresh_token"}
        if data.get("client_secret"):
            body["client_secret"] = decrypt(data["client_secret"])
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(TOKEN_URL, data=body)
        if response.status_code >= 400:
            raise GoogleError("Google authorization expired; reconnect the account", 401)
        tokens = response.json()
        access = tokens.get("access_token")
        if not access:
            raise GoogleError("Google did not return an access token", 502)
        data["access_token"] = encrypt(access)
        data["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
        _save(data)
        return access


async def request(api: str, method: str, path: str, *, params: dict | None = None,
                  body: dict | None = None, content: bytes | None = None,
                  content_type: str | None = None) -> dict | bytes:
    if api not in HOSTS or not path.startswith("/") or "//" in path or ".." in path:
        raise GoogleError("Invalid Google API request")
    url = HOSTS[api] + path
    for retry in range(2):
        token = await _token(force=bool(retry))
        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(method, url, params=params, json=body if content is None else None,
                                            content=content, headers=headers)
        if response.status_code == 401 and retry == 0:
            continue
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message") or response.text[:250]
            except (ValueError, AttributeError):
                detail = response.text[:250]
            raise GoogleError(f"Google API: {detail}", response.status_code)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content
    raise GoogleError("Google authorization expired; reconnect the account", 401)


async def list_files(*, query: str = "", parent: str | None = None, kind: str = "all",
                     trashed: bool = False, page_token: str | None = None) -> dict:
    terms = [f"trashed = {'true' if trashed else 'false'}"]
    if parent:
        terms.append(f"'{_id(parent)}' in parents")
    if kind in ("sheet", "form", "folder"):
        mime = {"sheet": "spreadsheet", "form": "form", "folder": "folder"}[kind]
        terms.append(f"mimeType = 'application/vnd.google-apps.{mime}'")
    elif kind != "all":
        raise GoogleError("Unknown file type")
    if query:
        safe = query[:100].replace("\\", "\\\\").replace("'", "\\'")
        terms.append(f"name contains '{safe}'")
    params = {"q": " and ".join(terms), "pageSize": 100, "fields": FILE_FIELDS,
              "orderBy": "folder,name", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
    if page_token:
        params["pageToken"] = page_token
    return await request("drive", "GET", "/files", params=params)


async def file_info(file_id: str) -> dict:
    return await request("drive", "GET", f"/files/{_id(file_id)}",
                         params={"fields": FILE_FIELDS.split("files(", 1)[1].rstrip(")"),
                                 "supportsAllDrives": "true"})


async def file_content(file_id: str, export_mime: str | None = None) -> bytes:
    path = f"/files/{_id(file_id)}"
    params = {"mimeType": export_mime} if export_mime else {"alt": "media", "supportsAllDrives": "true"}
    if export_mime:
        path += "/export"
    content = await request("drive", "GET", path, params=params)
    if not isinstance(content, bytes):
        raise GoogleError("Google returned no file content", 502)
    if len(content) > MAX_DOWNLOAD:
        raise GoogleError("This file is larger than Kairos's 25 MB download limit", 413)
    return content


async def drive_action(action: str, *, file_id: str | None = None, name: str | None = None,
                       parent: str | None = None, permission_id: str | None = None,
                       email: str | None = None, role: str | None = None,
                       permission_type: str | None = None, domain: str | None = None) -> dict:
    if action == "create_folder":
        return await request("drive", "POST", "/files", params={"fields": "id,name,mimeType,webViewLink"},
                             body={"name": (name or "New folder")[:200], "mimeType": "application/vnd.google-apps.folder",
                                   **({"parents": [_id(parent)]} if parent else {})})
    ident = _id(file_id or "")
    path = f"/files/{ident}"
    common = {"supportsAllDrives": "true", "fields": "id,name,mimeType,parents,trashed,starred,webViewLink"}
    if action == "rename":
        if not name or len(name) > 255:
            raise GoogleError("Enter a file name under 256 characters")
        return await request("drive", "PATCH", path, params=common, body={"name": name})
    if action == "move":
        if not parent:
            raise GoogleError("Choose a destination folder")
        info = await file_info(ident)
        return await request("drive", "PATCH", path,
                             params={**common, "addParents": _id(parent),
                                     "removeParents": ",".join(info.get("parents") or [])}, body={})
    if action == "copy":
        return await request("drive", "POST", path + "/copy", params=common,
                             body={**({"name": name[:255]} if name else {}),
                                   **({"parents": [_id(parent)]} if parent else {})})
    if action in ("trash", "restore"):
        return await request("drive", "PATCH", path, params=common, body={"trashed": action == "trash"})
    if action == "star":
        return await request("drive", "PATCH", path, params=common, body={"starred": True})
    if action == "unstar":
        return await request("drive", "PATCH", path, params=common, body={"starred": False})
    if action == "delete":
        await request("drive", "DELETE", path, params={"supportsAllDrives": "true"})
        return {"deleted": True}
    if action == "permissions":
        return await request("drive", "GET", path + "/permissions",
                             params={"supportsAllDrives": "true", "fields": "permissions(id,type,role,emailAddress,displayName,domain),nextPageToken"})
    if action == "share":
        kind = permission_type or "user"
        if kind not in ("user", "group", "domain", "anyone") or role not in ("reader", "commenter", "writer"):
            raise GoogleError("Choose a sharing type and reader, commenter, or writer access")
        if kind in ("user", "group") and (not email or "@" not in email or len(email) > 254):
            raise GoogleError("Enter a valid email address")
        if kind == "domain" and (not domain or len(domain) > 253 or not re.fullmatch(r"[A-Za-z0-9.-]+", domain)):
            raise GoogleError("Enter a valid domain")
        permission = {"type": kind, "role": role}
        if kind in ("user", "group"):
            permission["emailAddress"] = email
        elif kind == "domain":
            permission["domain"] = domain
        return await request("drive", "POST", path + "/permissions",
                             params={"supportsAllDrives": "true", "sendNotificationEmail": "true" if kind in ("user", "group") else "false",
                                     "fields": "id,type,role,emailAddress,domain"}, body=permission)
    if action == "update_permission":
        if role not in ("reader", "commenter", "writer"):
            raise GoogleError("Choose reader, commenter, or writer access")
        return await request("drive", "PATCH", path + f"/permissions/{_id(permission_id or '')}",
                             params={"supportsAllDrives": "true", "fields": "id,type,role,emailAddress,domain"},
                             body={"role": role})
    if action == "revoke":
        await request("drive", "DELETE", path + f"/permissions/{_id(permission_id or '')}",
                      params={"supportsAllDrives": "true"})
        return {"revoked": True}
    if action == "revisions":
        return await request("drive", "GET", path + "/revisions",
                             params={"fields": "revisions(id,modifiedTime,keepForever,lastModifyingUser,originalFilename),nextPageToken"})
    raise GoogleError("Unknown Drive action")


async def upload_file(name: str, data: bytes, mime_type: str, parent: str | None = None) -> dict:
    if not name or len(data) > MAX_DOWNLOAD:
        raise GoogleError("Choose a file under 25 MB", 413)
    boundary = "jarvis_" + secrets.token_hex(16)
    metadata = {"name": name[:255], **({"parents": [_id(parent)]} if parent else {})}
    payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
               + json.dumps(metadata) + f"\r\n--{boundary}\r\nContent-Type: {mime_type or 'application/octet-stream'}\r\n\r\n").encode("utf-8")
    payload += data + f"\r\n--{boundary}--\r\n".encode("ascii")
    return await request("upload", "POST", "/files", params={"uploadType": "multipart", "fields": "id,name,mimeType,webViewLink"},
                         content=payload, content_type=f"multipart/related; boundary={boundary}")


async def sheet_get(file_id: str, cell_range: str | None = None) -> dict:
    ident = _id(file_id)
    if cell_range:
        return await request("sheets", "GET", f"/spreadsheets/{ident}/values/{quote(cell_range, safe='!:$')}" ,
                             params={"valueRenderOption": "FORMATTED_VALUE"})
    return await request("sheets", "GET", f"/spreadsheets/{ident}",
                         params={"fields": "spreadsheetId,spreadsheetUrl,properties(title),sheets(properties(sheetId,title,index,gridProperties))"})


async def sheet_action(action: str, *, file_id: str | None = None, title: str | None = None,
                       cell_range: str | None = None, values: list | None = None,
                       requests: list | None = None) -> dict:
    if action == "create":
        return await request("sheets", "POST", "/spreadsheets", body={"properties": {"title": title or "Untitled spreadsheet"}})
    ident = _id(file_id or "")
    if action == "batch":
        if not isinstance(requests, list) or len(requests) > 50:
            raise GoogleError("Provide at most 50 spreadsheet changes")
        return await request("sheets", "POST", f"/spreadsheets/{ident}:batchUpdate", body={"requests": requests})
    if not cell_range or len(cell_range) > 300:
        raise GoogleError("Choose a cell range")
    path = f"/spreadsheets/{ident}/values/{quote(cell_range, safe='!:$')}"
    if action == "clear":
        return await request("sheets", "POST", path + ":clear", body={})
    if not isinstance(values, list) or len(values) > 1000:
        raise GoogleError("Provide at most 1,000 rows")
    if action == "update":
        return await request("sheets", "PUT", path, params={"valueInputOption": "USER_ENTERED"},
                             body={"range": cell_range, "majorDimension": "ROWS", "values": values})
    if action == "append":
        return await request("sheets", "POST", path + ":append",
                             params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                             body={"values": values})
    raise GoogleError("Unknown Sheets action")


async def form_get(file_id: str, responses: bool = False, page_token: str | None = None) -> dict:
    path = f"/forms/{_id(file_id)}" + ("/responses" if responses else "")
    return await request("forms", "GET", path, params={"pageSize": 100, **({"pageToken": page_token} if page_token else {})} if responses else None)


async def form_action(action: str, *, file_id: str | None = None, title: str | None = None,
                      requests: list | None = None, published: bool | None = None) -> dict:
    if action == "create":
        if not title:
            raise GoogleError("Enter a form title")
        return await request("forms", "POST", "/forms", body={"info": {"title": title[:200]}})
    ident = _id(file_id or "")
    if action == "batch":
        if not isinstance(requests, list) or len(requests) > 50:
            raise GoogleError("Provide at most 50 form changes")
        return await request("forms", "POST", f"/forms/{ident}:batchUpdate", body={"requests": requests})
    if action == "publish":
        if published is None:
            raise GoogleError("Choose a published state")
        return await request("forms", "POST", f"/forms/{ident}:setPublishSettings",
                             body={"publishSettings": {"publishState": {
                                 "isPublished": published, "isAcceptingResponses": published}},
                                   "updateMask": "publishState"})
    raise GoogleError("Unknown Forms action")
