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

MCP servers, roadmap phase 6 (2026-10-06; spec: the vault note "Skills and
Integrations - Phase 6 (Build Spec)"):
- Health: each check of a server (Tool Store's Check, adding it, a chat
  connecting, the task loop every CHECK_EVERY_SECONDS) is kept as its
  `status`: working with its tool count, down with the error, or signed out.
- Pinning: the first good check records each tool's fingerprint (its name,
  description and input schema). A tool that later appears, or whose
  description or schema changes, is held: no model is offered it until a
  person accepts it in Tool Store. MCP tool descriptions are read by the
  model, so a rewritten one is a way to slip instructions in; this stops a
  server changing its tools underneath you.
- A clean environment: a stdio server starts through
  mcp_servers/clean_launch.py, which passes it only safe system variables
  and its own key, on every model kind. Claude Code used to start it with
  the whole environment the backend had.
"""
import hashlib
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

INTEGRATIONS_FILE = os.path.join(DATA_DIR, "integrations.json")
CLEAN_LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_servers", "clean_launch.py")
CHECK_EVERY_SECONDS = 30 * 60

KINDS = {"api_service", "mcp_server", "caldav_calendar", "carddav_contacts", "ical_feed"}
MCP_TYPES = {"stdio", "http"}


def _load() -> dict:
    return read_json(INTEGRATIONS_FILE, {})


def _masked(item: dict) -> dict:
    out = {"id": item["id"], "kind": item["kind"], "name": item["name"]}
    out["enabled"] = item.get("enabled", True)
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
        out["auth"] = item.get("auth") or ("key" if item.get("api_key_encrypted") else "none")
        out["signed_in"] = bool((item.get("oauth") or {}).get("tokens"))
        out["status"] = item.get("status")
        out["pinned"] = "pinned_tools" in item
        out["held_tools"] = [{"name": name, **{k: held[k] for k in ("kind", "description")}}
                             for name, held in sorted((item.get("held_tools") or {}).items())]
    elif item["kind"] in ("caldav_calendar", "carddav_contacts", "ical_feed"):
        out["url"] = item.get("url", "")
        out["username"] = item.get("username", "")
        out["last_synced_count"] = item.get("last_synced_count")
    return out


def list_integrations() -> list[dict]:
    return [_masked(i) for i in _load().values()]


CATALOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_catalog.json")


def mcp_catalog() -> list[dict]:
    """Known MCP servers (core/mcp_catalog.json, adapted from Hermes Agent's
    catalog), each marked with whether it is already added here. Those that
    need no sign-in can be added in one step; OAuth ones are added and then
    signed in to (core/mcp_oauth.py). Either way it is an ordinary MCP Tool
    Server integration."""
    import json
    with open(CATALOG_FILE, encoding="utf-8") as f:
        servers = json.load(f)["servers"]
    added = {(i.get("url") or "").rstrip("/") for i in _load().values() if i.get("kind") == "mcp_server"}
    return [{**s, "added": (s.get("url") or "").rstrip("/") in added} for s in servers]


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
        if item["kind"] != "mcp_server" or not item.get("enabled", True):
            continue
        if only_ids is not None and item["id"] not in only_ids:
            continue
        api_key = decrypt(item["api_key_encrypted"]) if item.get("api_key_encrypted") else None
        if item.get("mcp_type") == "stdio":
            # Through the clean launcher (the module docstring), whoever starts it.
            cfg: dict[str, Any] = {"type": "stdio", "command": sys.executable,
                                   "args": ["-I", CLEAN_LAUNCHER, "--", item["command"], *(item.get("args") or [])]}
            if api_key:
                cfg["env"] = {"MCP_API_KEY": api_key}
        else:
            cfg = {"type": "http", "url": item["url"]}
            if item.get("auth") == "oauth":
                # Signed in through core/mcp_oauth.py, which keeps the token
                # fresh. Not signed in: left out, rather than handed to a
                # model as a server that can only answer 401.
                api_key = _oauth_access_token(item)
                if not api_key:
                    continue
            if api_key:
                cfg["headers"] = {"Authorization": f"Bearer {api_key}"}
        servers[item["name"]] = cfg
    return servers


# -- MCP health and pinned tools (phase 6) ---------------------------------------

def tool_fingerprint(tool: dict) -> str:
    blob = json.dumps([tool.get("name"), tool.get("description") or "", tool.get("schema") or {}], sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _mcp_item(data: dict, item_id: Optional[str] = None, name: Optional[str] = None) -> Optional[dict]:
    for item in data.values():
        if item.get("kind") == "mcp_server" and (item["id"] == item_id or (name is not None and item["name"] == name)):
            return item
    return None


def record_check(item_id: Optional[str] = None, tools: Optional[list[dict]] = None, error: Optional[str] = None,
                 signed_out: bool = False, name: Optional[str] = None) -> Optional[dict]:
    """Keep one check of a server: its status, and with a good tool list its
    pins and held tools. The first good check pins every tool it found.
    Returns the masked record, or None for an unknown server."""
    data = _load()
    item = _mcp_item(data, item_id, name)
    if item is None:
        return None
    now = time.time()
    if signed_out:
        item["status"] = {"state": "signed_out", "tools": None, "checked_at": now, "error": None}
    elif tools is None:
        item["status"] = {"state": "down", "tools": None, "checked_at": now, "error": (error or "no answer")[:300]}
    else:
        current = {t["name"]: {"fingerprint": tool_fingerprint(t), "description": (t.get("description") or "")[:500]}
                   for t in tools}
        if "pinned_tools" not in item:
            item["pinned_tools"] = {n: c["fingerprint"] for n, c in current.items()}
        pinned = item["pinned_tools"]
        item["held_tools"] = {n: {**c, "kind": "new" if n not in pinned else "changed"}
                              for n, c in current.items() if pinned.get(n) != c["fingerprint"]}
        item["status"] = {"state": "working", "tools": len(current) - len(item["held_tools"]), "checked_at": now,
                          "error": None}
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(item)


def held_tools(only_ids: Optional[list[str]] = None) -> dict[str, set]:
    """Held tool names by server name, for the servers a chat may use."""
    return {item["name"]: set(item.get("held_tools") or {}) for item in _load().values()
            if item.get("kind") == "mcp_server" and (only_ids is None or item["id"] in only_ids)
            and item.get("held_tools")}


def claude_tool_name(server: str, tool: str) -> str:
    """The name Claude Code gives an MCP tool: mcp__<server>__<tool>, with
    anything outside letters, digits, _ and - made an underscore."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{server}__{tool}")


def accept_tools(item_id: str, names: list[str]) -> dict:
    """Pin these held tools as they were when the person reviewed them. A
    tool that changed again since stays held."""
    data = _load()
    item = _mcp_item(data, item_id)
    if item is None:
        raise KeyError(item_id)
    held = item.get("held_tools") or {}
    unknown = [n for n in names if n not in held]
    if unknown:
        raise ValueError(f"not waiting for review: {', '.join(unknown)}")
    for name in names:
        item.setdefault("pinned_tools", {})[name] = held.pop(name)["fingerprint"]
    if item.get("status", {}).get("state") == "working":
        item["status"]["tools"] = (item["status"].get("tools") or 0) + len(names)
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(item)


def _oauth_access_token(item: dict) -> Optional[str]:
    import json
    raw = (item.get("oauth") or {}).get("tokens")
    return json.loads(decrypt(raw)).get("access_token") if raw else None


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
                       api_key: Optional[str] = None, auth: Optional[str] = None, *, replace_id=None) -> dict:
    """`auth="oauth"` marks a server that is used only once signed in
    (core/mcp_oauth.py); the catalog's sign-in servers are added that way."""
    if mcp_type not in MCP_TYPES:
        raise ValueError("mcp_type must be 'stdio' or 'http'")
    if mcp_type == "stdio" and not command:
        raise ValueError("command is required for a stdio MCP server")
    if mcp_type == "http" and not url:
        raise ValueError("url is required for an http MCP server")
    if auth not in (None, "oauth") or (auth == "oauth" and mcp_type != "http"):
        raise ValueError("auth may only be 'oauth', and only for an http MCP server")
    data = _load()
    previous = data.get(replace_id) if replace_id else None
    if replace_id and (not previous or previous["kind"] != "mcp_server"):
        raise ValueError("Store update requires an existing MCP server")
    item_id = replace_id or uuid.uuid4().hex[:12]
    data[item_id] = {
        "id": item_id, "kind": "mcp_server", "name": name, "mcp_type": mcp_type,
        "command": command, "args": args or [], "url": url,
        "api_key_encrypted": encrypt(api_key) if api_key else None,
    }
    if auth:
        data[item_id]["auth"] = auth
    if previous:
        for key in ("pinned_tools", "enabled"):
            if key in previous:
                data[item_id][key] = previous[key]
        if previous.get("url") == url and previous.get("auth") == auth and previous.get("oauth"):
            data[item_id]["oauth"] = previous["oauth"]
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


def set_enabled(item_id: str, enabled: bool) -> dict:
    """Suspend a server while preserving credentials and tool acceptance."""
    data = _load()
    if item_id not in data:
        raise KeyError(item_id)
    data[item_id]["enabled"] = enabled
    write_json_atomic(INTEGRATIONS_FILE, data)
    return _masked(data[item_id])


def get_integration(item_id: str) -> Optional[dict]:
    return _load().get(item_id)


def get_integration_masked(item_id: str) -> Optional[dict]:
    item = _load().get(item_id)
    return _masked(item) if item else None
