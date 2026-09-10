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

File attachments (added 2026-09-08, David's ask, porting voice-line's
discord_bot.py capability): any file type dropped into an allowed channel
gets staged via core/attachments.py's stage_file()/resolve_for_turn() —
the same pipeline the web Chat composer's own upload button uses — so a
Discord upload lands the model in the exact same place a browser upload
does: copied into the session's cwd, read with its normal file tools.
Unlike voice-line's version, this doesn't keep a separate
discord_attachments/ folder or write its own path-listing message — it
reuses chat_service.py's existing attachment handling instead of a second
implementation of the same idea.

Outbound file sending added 2026-09-10 (David's ask: image generation for
both Discord and chat) — see _extract_generated_images(). Still scoped
narrowly: only the app's own generate_image tool output gets attached back;
nothing else about outbound sending changed.
"""
import asyncio
import logging
import os
import re

from core import attachments, discord_bots_store, events, image_gen
from core.session_manager import session_manager
from services import chat_service

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000

# Same pattern chat.js parses client-side (David's ask 2026-09-10: image
# generation for chat and Discord) — the generate_image tool always emits
# this exact markdown shape. Discord has no way to fetch a
# /generated-images/... path itself (it isn't a public URL), so this bot has
# to pull the real bytes off disk and attach them as a genuine file.
GENERATED_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((/generated-images/[^\s)]+)\)")


def _extract_generated_images(text: str) -> tuple[str, list[str]]:
    """Returns (text with the markdown stripped out, [local file paths]).
    Missing files are skipped rather than raising — a file that somehow
    isn't on disk shouldn't break the rest of the reply."""
    paths = []

    def _strip(match: "re.Match") -> str:
        filename = os.path.basename(match.group(2))
        candidate = os.path.join(image_gen.GENERATED_DIR, filename)
        if os.path.isfile(candidate):
            paths.append(candidate)
        return ""

    # Collapse the whitespace a stripped-out inline image leaves behind
    # ("Two:  and" from "Two: ![x](...) and") into something that reads
    # naturally, without touching intentional paragraph breaks.
    cleaned = re.sub(r"[ \t]+", " ", GENERATED_IMAGE_RE.sub(_strip, text)).strip()
    return cleaned, paths
# Same cap the web Chat composer's upload button enforces
# (routes/chat_routes.py's _MAX_ATTACHMENT_BYTES) — one shared limit for
# both entry points into core/attachments.py's staging pipeline.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
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


def _should_respond(channel_id: str, author_id: str, channel_modes: dict, allowed_user_id) -> bool:
    """Pure so it's testable without a real discord.Client. channel_modes is
    {discord_channel_id: mode}, mode one of discord_bots_store.CHANNEL_MODES.
    A channel not in the dict (including every DM) keeps the original
    behavior: gated by allowed_user_id if one is set, open to anyone
    otherwise. "silent" always wins over "open" if a channel were ever
    misconfigured as both — checked first, unconditionally."""
    mode = channel_modes.get(channel_id, "normal")
    if mode == "silent":
        return False
    if mode == "open":
        return True
    return not allowed_user_id or author_id == allowed_user_id


def _build_client(discord, bot: dict):
    channel_key = f"discord:{bot['id']}"
    allowed_user_id = bot.get("allowed_user_id")
    model_endpoint_id = bot.get("model_endpoint_id")
    channel_modes = {c["discord_channel_id"]: c["mode"] for c in bot.get("channels", [])}

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
        if not _should_respond(str(message.channel.id), str(message.author.id), channel_modes, allowed_user_id):
            return
        # A message can be attachments with no text — Discord allows that.
        # Without this, an empty message.content still reached
        # chat_service.send_message() as a blank turn.
        if not message.content.strip() and not message.attachments:
            return

        # Any file type, matching The Bridge's discord_bot.py convention —
        # this bot's job is only to get the bytes staged, not to parse them.
        # stage_file()/resolve_for_turn() is the same pipeline the web Chat
        # composer's own attach-files button uses (see core/attachments.py),
        # so a Discord upload and a browser upload land the model in the
        # exact same place: copied into the session's cwd, read with its
        # normal file tools.
        attachment_ids: list[str] = []
        if message.attachments:
            for att in message.attachments:
                if att.size > MAX_ATTACHMENT_BYTES:
                    await message.channel.send(f"Couldn't attach {att.filename}: file too large (25MB max)")
                    continue
                try:
                    content = await att.read()
                    staged = attachments.stage_file(att.filename, content)
                    attachment_ids.append(staged["id"])
                except Exception as e:
                    logger.exception("discord_channel: %s failed to download an attachment", bot["name"])
                    await message.channel.send(f"Couldn't download {att.filename}: {e}")

        session_id = session_manager.get_or_create_channel_session(
            channel_key, bot["name"], model_endpoint_id=model_endpoint_id,
        )
        async with message.channel.typing():
            try:
                reply = await chat_service.send_message(session_id, message.content, attachment_ids)
            except Exception:
                logger.exception("discord_channel: %s failed to process message", bot["name"])
                reply = "Something went wrong on my end handling that — check the app logs."

        # Found live 2026-09-01: this send was unguarded, so a failure here
        # (a network blip, a stale channel reference, a transient Discord
        # API error) after a long-running turn silently dropped the reply
        # with no trace anywhere.
        try:
            text, image_paths = _extract_generated_images(reply)
            # A reply that's nothing but the image markdown leaves text
            # empty after stripping it - Discord rejects a genuinely empty
            # message (no content, no embed, no attachment yet at this
            # point), so only send if there's real text left.
            if text:
                for chunk in _chunk_message(text):
                    await message.channel.send(chunk)
            # Real attachments, not a link to a local path Discord could
            # never fetch itself (see GENERATED_IMAGE_RE's comment) - each
            # sent as its own message so multiple images in one reply don't
            # get silently dropped by Discord's per-message attachment cap.
            for path in image_paths:
                await message.channel.send(file=discord.File(path))
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


async def send_to_named_channel(bot_id: str, entry_id: str, text: str) -> bool:
    """Proactive send to one of a bot's configured named channels (David's
    ask 2026-09-10: always post the daily brief to a specific channel, not
    just DM the allowed user). Returns False on any failure or unknown
    bot/channel/client, same "delivery miss shouldn't take the caller down"
    contract as send_direct_message."""
    client = _clients.get(bot_id)
    if not client:
        return False
    bot = discord_bots_store.get_bot(bot_id)
    if not bot:
        return False
    entry = next((c for c in bot.get("channels", []) if c["id"] == entry_id), None)
    if not entry:
        return False
    try:
        channel_id = int(entry["discord_channel_id"])
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        for chunk in _chunk_message(text):
            await channel.send(chunk)
        return True
    except Exception:
        logger.exception("discord_channel: send_to_named_channel failed for bot %s channel entry %s", bot_id, entry_id)
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
