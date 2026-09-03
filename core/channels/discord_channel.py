"""Discord comms channel — Phase 4's first adapter (David's onboarding
decision, 2026-08-31: Discord is the prompted default, but the core stays
channel-agnostic so Telegram/others are one more adapter, not a rewrite).

Unlike the v1 starter kit's channels/discord/bot.py (a separate standalone
process with its own bare Brain and no persistence), this runs inside the
same FastAPI process as an asyncio task and routes every message through the
real services/chat_service.py — so a Discord conversation gets the same
session persistence, vault-backed memory, and skills as any other chat, and
shows up in the normal Chats sidebar (one shared session per channel, via
session_manager.get_or_create_channel_session()) rather than being a
second, disconnected conversation store.

Multiple bots added 2026-09-01 (David's ask: "you should be able to see all
the bots you have connected to" — the original design only ever supported
one). Each configured bot (core/discord_bots_store.py) gets its own real
discord.Client, its own channel-session key (so two bots' conversations
don't collide into one shared session), and its own default model endpoint
— set explicitly in Settings, since a channel session otherwise has no
model chosen at all and chat_service.py's NO_MODEL_MESSAGE is exactly what
David hit messaging a connected bot with nothing configured.

Degrades gracefully: with no bots configured, start() is a no-op — matches
the "only wire what's configured" principle from the v1 wizard.
"""
import asyncio
import logging

from core import discord_bots_store, events
from core.session_manager import session_manager
from services import chat_service

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
# Found live 2026-09-01: a bare fire-and-forget client.start() task has
# nowhere to send an exception, so one bad turn or a transient Discord
# hiccup killed the bot for good with zero visibility. Backing off between
# restart attempts keeps a persistently bad token from spin-looping.
RESTART_BACKOFF_SECONDS = 30

_clients: dict[str, object] = {}  # bot_id -> discord.Client
_tasks: dict[str, "asyncio.Task"] = {}  # bot_id -> supervisor task


def _chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Line-boundary-first chunking, matching voice-line's own discord_bot.py
    convention — Discord rejects any single message over 2000 characters."""
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


async def start() -> None:
    bots = discord_bots_store.list_bots()
    if not bots:
        logger.info("discord_channel: no bots configured, skipping (no-op by design)")
        return

    try:
        import discord
    except ImportError:
        logger.warning("discord_channel: bot(s) configured but the 'discord.py' package isn't installed")
        return

    for bot in bots:
        await _start_one(discord, bot)


def _build_client(discord, bot: dict):
    channel_key = f"discord:{bot['id']}"
    allowed_user_id = bot.get("allowed_user_id")
    model_endpoint_id = bot.get("model_endpoint_id")

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info("discord_channel: %s logged in as %s", bot["name"], client.user)
        events.emit("channel.connected", f"Discord bot {bot['name']} connected", bot_id=bot["id"])

    @client.event
    async def on_message(message):
        if message.author == client.user:
            return
        if allowed_user_id and str(message.author.id) != allowed_user_id:
            return

        session_id = session_manager.get_or_create_channel_session(
            channel_key, bot["name"], model_endpoint_id=model_endpoint_id,
        )
        async with message.channel.typing():
            try:
                reply = await chat_service.send_message(session_id, message.content)
            except Exception:
                logger.exception("discord_channel: %s failed to process message", bot["name"])
                reply = "Something went wrong on my end handling that — check the app logs."

        # Found live 2026-09-01: this send was unguarded, so a failure here
        # (a network blip, a stale channel reference, a transient Discord
        # API error) after a long-running turn silently dropped the reply
        # with no trace anywhere.
        try:
            for chunk in _chunk_message(reply):
                await message.channel.send(chunk)
        except Exception:
            logger.exception("discord_channel: %s failed to send its reply", bot["name"])

    return client


async def _run_supervised(discord, bot: dict, token: str) -> None:
    while True:
        client = _build_client(discord, bot)
        _clients[bot["id"]] = client
        try:
            await client.start(token)
            logger.warning("discord_channel: %s's connection ended, restarting in %ss",
                            bot["name"], RESTART_BACKOFF_SECONDS)
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception:
            logger.exception("discord_channel: %s crashed, restarting in %ss",
                              bot["name"], RESTART_BACKOFF_SECONDS)
            events.emit("channel.crashed", f"Discord bot {bot['name']} crashed, restarting", level="warn", bot_id=bot["id"])
        finally:
            _clients.pop(bot["id"], None)
        await asyncio.sleep(RESTART_BACKOFF_SECONDS)


async def _start_one(discord, bot: dict) -> None:
    token = discord_bots_store.resolve_token(bot["id"])
    if not token:
        return

    _tasks[bot["id"]] = asyncio.create_task(_run_supervised(discord, bot, token))
    logger.info("discord_channel: starting %s (allowlist=%s, model=%s)",
                bot["name"], "on" if bot.get("allowed_user_id") else "off",
                "set" if bot.get("model_endpoint_id") else "none")


async def send_direct_message(text: str) -> bool:
    """Proactive send (David's ask 2026-08-31: task output delivered to a
    comms channel) — DMs whichever configured bot has an allowed_user_id
    set (the one Discord identity that bot already trusts), first match.
    Returns False (not an exception) on any failure — a delivery miss
    shouldn't take down whatever called it (e.g. the task scheduler)."""
    for bot in discord_bots_store.list_bots():
        client = _clients.get(bot["id"])
        allowed_user_id = bot.get("allowed_user_id")
        if not client or not allowed_user_id:
            continue
        try:
            user = await client.fetch_user(int(allowed_user_id))
            for chunk in _chunk_message(text):
                await user.send(chunk)
            return True
        except Exception:
            logger.exception("discord_channel: send_direct_message failed for %s", bot["name"])
    return False


def connected_bots() -> list[str]:
    """Names of bots with a live client whose gateway connection is actually
    up (not just a supervisor task that exists) — /api/system/status's live
    Discord health, distinct from diagnostics' static "configured" bool."""
    live = []
    for bot in discord_bots_store.list_bots():
        client = _clients.get(bot["id"])
        # is_ready(), not is_closed() — is_closed() is False from construction
        # onward, so it would report "connected" while still logging in or
        # mid-reconnect; is_ready() only flips true once the gateway session
        # is actually established.
        if client is not None and getattr(client, "is_ready", lambda: False)():
            live.append(bot["name"])
    return live


async def stop() -> None:
    # Cancel the supervisor tasks first — otherwise a deliberate stop just
    # looks like a crash to _run_supervised and it restarts the bot right
    # back up.
    for bot_id, task in list(_tasks.items()):
        task.cancel()
    for task in list(_tasks.values()):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("discord_channel: error stopping a bot")
    _tasks.clear()
    _clients.clear()


async def restart() -> None:
    """Called by the Settings routes after adding/editing/removing a bot, so
    a config change takes effect immediately rather than needing a full app
    restart."""
    await stop()
    await start()
