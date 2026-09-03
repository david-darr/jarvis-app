"""Integrations — Settings > Integrations (David's ask 2026-08-31, matching
Odysseus's "Add Integration" panel — a real screenshot, not guessed).

Odysseus's dropdown offers 8 types: API Service, CalDAV Calendar, Claude
Agent, Codex Agent, Contacts (CardDAV), Contacts Import, Email (IMAP/SMTP),
MCP Tool Server. Built:

- api_service: a generic name/base-URL/API-key record.
- mcp_server: real — widens the agent's actual tool access (core/brain.py).
- caldav_calendar: real, one-way read sync via core/dav_client.py, merges
  into services/calendar_service.py tagged by sync_id (David's follow-up ask
  2026-08-31, after initially scoping this out for lack of any WebDAV
  client — built one, see dav_client.py for the real scope/limits).
- carddav_contacts: real, one-way read sync, stored in
  core/contacts_store.py and viewable from the Integrations panel itself
  (no dedicated Contacts tab exists yet).
- email: NOT a separate stored kind — Email already has its own tab/service
  (services/email_service.py) with full account CRUD; the Integrations
  panel's "Email (IMAP/SMTP)" entry just opens that tab rather than
  duplicating account storage.

Still not offered: Claude Agent (Claude is a hardcoded Claude Agent SDK
path, not an API-key config like the other providers) and Codex Agent
(not integrated anywhere) — no foundation, not built as fake UI.
"""
import os
import uuid
from typing import Any, Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

INTEGRATIONS_FILE = os.path.join(DATA_DIR, "integrations.json")

KINDS = {"api_service", "mcp_server", "caldav_calendar", "carddav_contacts", "ical_feed"}
MCP_TYPES = {"stdio", "http"}


def _load() -> dict:
    return read_json(INTEGRATIONS_FILE, {})


def _masked(item: dict) -> dict:
    out = {"id": item["id"], "kind": item["kind"], "name": item["name"]}
    if item["kind"] == "api_service":
        out["base_url"] = item.get("base_url", "")
        out["has_api_key"] = bool(item.get("api_key_encrypted"))
    elif item["kind"] == "mcp_server":
        out["mcp_type"] = item.get("mcp_type")
        if item.get("mcp_type") == "stdio":
            out["command"] = item.get("command", "")
            out["args"] = item.get("args", [])
        else:
            out["url"] = item.get("url", "")
        out["has_api_key"] = bool(item.get("api_key_encrypted"))
    elif item["kind"] in ("caldav_calendar", "carddav_contacts", "ical_feed"):
        out["url"] = item.get("url", "")
        out["username"] = item.get("username", "")
        out["last_synced_count"] = item.get("last_synced_count")
    return out


def list_integrations() -> list[dict]:
    return [_masked(i) for i in _load().values()]


def list_mcp_servers_runtime(only_ids: Optional[list[str]] = None) -> dict[str, dict]:
    """Live mcp_servers dict for ClaudeAgentOptions, keys are integration
    names, secrets decrypted — runtime use only, never returned from an API.

    `only_ids` restricts to specific integrations (David's ask 2026-08-31,
    Claude-style per-conversation connector toggle — see
    core/session_manager.py's enabled_integration_ids); None (the default)
    keeps every registered MCP server available, matching the original
    global behavior."""
    servers = {}
    for item in _load().values():
        if item["kind"] != "mcp_server":
            continue
        if only_ids is not None and item["id"] not in only_ids:
            continue
        api_key = decrypt(item["api_key_encrypted"]) if item.get("api_key_encrypted") else None
        if item.get("mcp_type") == "stdio":
            cfg: dict[str, Any] = {"type": "stdio", "command": item["command"]}
            if item.get("args"):
                cfg["args"] = item["args"]
            if api_key:
                cfg["env"] = {"MCP_API_KEY": api_key}
        else:
            cfg = {"type": "http", "url": item["url"]}
            if api_key:
                cfg["headers"] = {"Authorization": f"Bearer {api_key}"}
        servers[item["name"]] = cfg
    return servers


def create_api_service(name: str, base_url: str, api_key: Optional[str] = None) -> dict:
    data = _load()
    item_id = uuid.uuid4().hex[:12]
    data[item_id] = {
        "id": item_id, "kind": "api_service", "name": name,
        "base_url": base_url.rstrip("/"),
        "api_key_encrypted": encrypt(api_key) if api_key else None,
    }
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(data[item_id])


def create_mcp_server(name: str, mcp_type: str, command: Optional[str] = None,
                       args: Optional[list[str]] = None, url: Optional[str] = None,
                       api_key: Optional[str] = None) -> dict:
    if mcp_type not in MCP_TYPES:
        raise ValueError("mcp_type must be 'stdio' or 'http'")
    if mcp_type == "stdio" and not command:
        raise ValueError("command is required for a stdio MCP server")
    if mcp_type == "http" and not url:
        raise ValueError("url is required for an http MCP server")
    data = _load()
    item_id = uuid.uuid4().hex[:12]
    data[item_id] = {
        "id": item_id, "kind": "mcp_server", "name": name, "mcp_type": mcp_type,
        "command": command, "args": args or [], "url": url,
        "api_key_encrypted": encrypt(api_key) if api_key else None,
    }
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(data[item_id])


def create_dav(kind: str, name: str, url: str, username: str, password: str) -> dict:
    if kind not in ("caldav_calendar", "carddav_contacts"):
        raise ValueError("kind must be 'caldav_calendar' or 'carddav_contacts'")
    data = _load()
    item_id = uuid.uuid4().hex[:12]
    data[item_id] = {
        "id": item_id, "kind": kind, "name": name, "url": url.rstrip("/") + "/",
        "username": username, "password_encrypted": encrypt(password),
        "last_synced_count": None,
    }
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(data[item_id])


def get_dav_credentials(item_id: str) -> tuple[str, str, str]:
    """(url, username, password) with the password decrypted — runtime use
    (sync) only, never returned from an API."""
    item = _load().get(item_id)
    if item is None:
        raise KeyError(f"no such integration: {item_id}")
    return item["url"], item["username"], decrypt(item["password_encrypted"])


def create_ical_feed(name: str, url: str, username: Optional[str] = None, password: Optional[str] = None) -> dict:
    """Plain iCal (.ics) feed subscription (David's ask 2026-08-31) — a
    single specific resource URL, unlike CalDAV's collection URL, so no
    trailing slash is forced on it. Username/password optional since most
    public iCal feeds (Google's "secret address", Apple share links) need
    no auth at all."""
    data = _load()
    item_id = uuid.uuid4().hex[:12]
    data[item_id] = {
        "id": item_id, "kind": "ical_feed", "name": name, "url": url,
        "username": username or "", "password_encrypted": encrypt(password) if password else None,
        "last_synced_count": None,
    }
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(data[item_id])


def get_ical_credentials(item_id: str) -> tuple[str, Optional[str], Optional[str]]:
    item = _load().get(item_id)
    if item is None:
        raise KeyError(f"no such integration: {item_id}")
    password = decrypt(item["password_encrypted"]) if item.get("password_encrypted") else None
    return item["url"], (item.get("username") or None), password


def record_sync_count(item_id: str, count: int) -> None:
    data = _load()
    if item_id in data:
        data[item_id]["last_synced_count"] = count
        write_json_atomic(INTEGRATIONS_FILE, data)


def delete_integration(item_id: str) -> None:
    data = _load()
    data.pop(item_id, None)
    write_json_atomic(INTEGRATIONS_FILE, data)


def get_integration(item_id: str) -> Optional[dict]:
    return _load().get(item_id)


def get_integration_masked(item_id: str) -> Optional[dict]:
    item = _load().get(item_id)
    return _masked(item) if item else None
