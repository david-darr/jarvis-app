"""Multiple connected Discord bots (David's ask 2026-09-01: "you should be
able to see all the bots you have connected to") — the original design only
supported one bot via core/settings.py's single discord_bot_token_encrypted
key. JSON-backed list, same convention as core/model_endpoints.py. Each bot
gets its own token, optional allowed-user-id lock, and optional default
model endpoint (so messaging it doesn't hit chat_service's "no model added"
message — a session-per-channel has no model unless one is actually set).
"""
import os
import uuid
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

BOTS_FILE = os.path.join(DATA_DIR, "discord_bots.json")


def _load() -> dict:
    """Self-healing one-time migration (same pattern as
    core/model_endpoints.py's num_ctx backfill): the original single-bot
    design stored its token under core/settings.py's
    discord_bot_token_encrypted key — a real bot David already had
    configured there before this file existed. Migrated in automatically on
    first load rather than silently dropped, and only once (the old keys
    are cleared right after so this doesn't re-run or duplicate).

    Also remaps any real conversation history under the old single shared
    "discord" channel-session key onto the new per-bot "discord:<bot_id>"
    key — found live, 2026-09-01: without this, a real prior conversation
    (including the exact one that prompted this whole fix — "hello?" /
    the "no model added" reply) would silently orphan behind the new
    scheme, and the next message would start a brand new, empty session
    instead of continuing it."""
    data = read_json(BOTS_FILE, {})
    if not data:
        from core import settings as settings_store
        legacy_token = settings_store.get_setting("discord_bot_token_encrypted")
        if legacy_token:
            bot_id = uuid.uuid4().hex[:12]
            data[bot_id] = {
                "id": bot_id,
                "name": "Discord Bot",
                "token_encrypted": legacy_token,
                "allowed_user_id": settings_store.get_setting("discord_allowed_user_id"),
                "model_endpoint_id": None,
            }
            write_json_atomic(BOTS_FILE, data)
            settings_store.update_settings(discord_bot_token_encrypted=None, discord_allowed_user_id=None)

            from core.atomic_io import read_json as _read_json, write_json_atomic as _write_json_atomic
            from core.constants import DATA_DIR as _DATA_DIR
            channel_sessions_file = os.path.join(_DATA_DIR, "channel_sessions.json")
            mapping = _read_json(channel_sessions_file, {})
            if "discord" in mapping:
                mapping[f"discord:{bot_id}"] = mapping.pop("discord")
                _write_json_atomic(channel_sessions_file, mapping)
    return data


def _masked(bot: dict) -> dict:
    return {
        "id": bot["id"],
        "name": bot["name"],
        "allowed_user_id": bot.get("allowed_user_id"),
        "model_endpoint_id": bot.get("model_endpoint_id"),
        "has_token": bool(bot.get("token_encrypted")),
    }


def list_bots() -> list[dict]:
    return [_masked(b) for b in _load().values()]


def get_bot(bot_id: str) -> Optional[dict]:
    return _load().get(bot_id)


def create_bot(name: str, token: str, allowed_user_id: Optional[str] = None,
                model_endpoint_id: Optional[str] = None) -> dict:
    data = _load()
    bot_id = uuid.uuid4().hex[:12]
    data[bot_id] = {
        "id": bot_id,
        "name": name,
        "token_encrypted": encrypt(token),
        "allowed_user_id": allowed_user_id or None,
        "model_endpoint_id": model_endpoint_id or None,
    }
    write_json_atomic(BOTS_FILE, data)
    return _masked(data[bot_id])


def update_bot(bot_id: str, name: Optional[str] = None, token: Optional[str] = None,
                allowed_user_id: Optional[str] = None, model_endpoint_id: Optional[str] = None) -> dict:
    data = _load()
    bot = data.get(bot_id)
    if bot is None:
        raise KeyError(f"no such bot: {bot_id}")
    if name is not None:
        bot["name"] = name
    if token:
        bot["token_encrypted"] = encrypt(token)
    if allowed_user_id is not None:
        bot["allowed_user_id"] = allowed_user_id or None
    if model_endpoint_id is not None:
        bot["model_endpoint_id"] = model_endpoint_id or None
    write_json_atomic(BOTS_FILE, data)
    return _masked(bot)


def delete_bot(bot_id: str) -> None:
    data = _load()
    data.pop(bot_id, None)
    write_json_atomic(BOTS_FILE, data)


def resolve_token(bot_id: str) -> Optional[str]:
    bot = get_bot(bot_id)
    if bot is None or not bot.get("token_encrypted"):
        return None
    return decrypt(bot["token_encrypted"])
