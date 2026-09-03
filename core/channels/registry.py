"""Channel registry — Settings > Channels (David's ask 2026-08-31: rename
Discord's Settings section to "Channels" and make it a real place to add
secondary comms channels, not a Discord-only panel). Discord is the only
real channel today (core/channels/discord_channel.py); this registry exists
so a second channel later is one more entry, not a rewrite of Settings or
the task-delivery wiring below — not a fake multi-channel picker with
options that don't work.
"""
from core import discord_bots_store
from core.channels import discord_channel


def list_channels() -> list[dict]:
    return [
        {
            "id": "discord",
            "label": "Discord",
            "configured": len(discord_bots_store.list_bots()) > 0,
        },
    ]


async def send_to_channel(channel_id: str, text: str) -> bool:
    """Best-effort proactive delivery (task output, David's ask 2026-08-31).
    Returns False rather than raising on any failure/unknown channel — a
    delivery miss shouldn't fail whatever produced the text."""
    if channel_id == "discord":
        return await discord_channel.send_direct_message(text)
    return False
