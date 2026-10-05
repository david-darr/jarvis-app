"""Slack, through Socket Mode (a WebSocket the app opens, so no public URL
is needed) and the Web API. Reference: Hermes plugins/platforms/slack (MIT),
which uses Slack Bolt; this talks to the same endpoints directly.

Setup: a Slack app with Socket Mode on, an app-level token (connections:
write) and a bot token (chat:write, channels:history, im:history,
groups:history), subscribed to message.channels / message.im events."""
import json
import re

from core.connectors import register
from core.connectors.base import Connector, ConnectorError, Field, Inbound, require

API = "https://slack.com/api"
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


def _slack(response, what: str) -> dict:
    data = require(response, what)
    if not data.get("ok", False):
        raise ConnectorError(f"{what} failed: {data.get('error', 'unknown error')}")
    return data


@register
class Slack(Connector):
    kind = "slack"
    label = "Slack"
    description = "A Slack app in your workspace, connected with Socket Mode (no public URL)."
    docs_url = "https://api.slack.com/apis/socket-mode"
    message_limit = 3900
    target_field = "default_channel"
    sender_help = "Slack member IDs (Profile > More > Copy member ID, like U01ABC2DEF), one per line."
    fields = (
        Field("app_token", "App-level token", secret=True, help="Basic Information > App-Level Tokens, with connections:write.",
              placeholder="xapp-..."),
        Field("bot_token", "Bot token", secret=True, help="OAuth & Permissions > Bot User OAuth Token.", placeholder="xoxb-..."),
        Field("default_channel", "Channel for notifications", required=False,
              help="A channel ID (like C01ABC2DEF) where task results and agent notifications go."),
    )

    def connect(self, url: str):
        from websockets.asyncio.client import connect
        return connect(url, max_size=None)

    async def run(self) -> None:
        async with self.http(headers={"Authorization": f"Bearer {self.setting('bot_token')}"}) as api:
            self.bot_user = _slack(await api.post(f"{API}/auth.test"), "Checking the bot token").get("user_id")
            opened = _slack(await self._post_app(f"{API}/apps.connections.open"), "Opening Socket Mode")
            async with self.connect(opened["url"]) as socket:
                async for raw in socket:
                    envelope = json.loads(raw)
                    if envelope.get("envelope_id"):
                        await socket.send(json.dumps({"envelope_id": envelope["envelope_id"]}))
                    if envelope.get("type") == "disconnect":
                        raise ConnectorError("Slack asked to reconnect")
                    event = (envelope.get("payload") or {}).get("event") or {}
                    if envelope.get("type") == "events_api" and event.get("type") == "message":
                        inbound = await self._inbound(api, event)
                        if inbound:
                            await self.hub.receive(self, inbound)

    async def _post_app(self, url: str):
        async with self.http(headers={"Authorization": f"Bearer {self.setting('app_token')}"}) as app:
            return await app.post(url)

    async def _inbound(self, api, event: dict):
        if event.get("bot_id") or event.get("subtype") or event.get("user") == getattr(self, "bot_user", None):
            return None  # our own messages, edits, joins
        thread, ts = event.get("thread_ts"), event.get("ts")
        replying_to = ""
        if thread and thread != ts:
            parent = await api.get(f"{API}/conversations.replies", params={"channel": event["channel"], "ts": thread, "limit": 1})
            first = ((parent.json() if parent.status_code == 200 else {}).get("messages") or [{}])[0]
            if first.get("user") == self.bot_user or first.get("bot_id"):
                replying_to = first.get("text", "")
        return Inbound(conversation=event["channel"], sender=event.get("user", ""),
                       text=_MENTION.sub("", event.get("text") or ""), replying_to=replying_to)

    async def send(self, conversation: str, text: str) -> None:
        async with self.http(headers={"Authorization": f"Bearer {self.setting('bot_token')}"}) as api:
            _slack(await api.post(f"{API}/chat.postMessage", json={"channel": conversation, "text": text}),
                   "Sending to Slack")
