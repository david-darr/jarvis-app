"""Mattermost, through its WebSocket event stream and REST API v4 with a
bot or personal access token. Reference: Hermes plugins/platforms/mattermost
(MIT)."""
import json

from core.connectors import register
from core.connectors.base import Connector, ConnectorError, Field, Inbound, require


@register
class Mattermost(Connector):
    kind = "mattermost"
    label = "Mattermost"
    description = "A Mattermost bot account on your server."
    docs_url = "https://developers.mattermost.com/integrate/reference/bot-accounts/"
    message_limit = 15000
    target_field = "default_channel"
    sender_help = "Mattermost user IDs (System Console > Users, or the API), one per line."
    fields = (
        Field("server_url", "Server URL", placeholder="https://chat.example.com"),
        Field("token", "Bot access token", secret=True),
        Field("default_channel", "Channel ID for notifications", required=False),
    )

    @property
    def base(self) -> str:
        return self.setting("server_url").rstrip("/")

    def connect(self, url: str):
        from websockets.asyncio.client import connect
        return connect(url, max_size=None)

    async def run(self) -> None:
        token = self.setting("token")
        async with self.http(headers={"Authorization": f"Bearer {token}"}) as api:
            self.me = require(await api.get(f"{self.base}/api/v4/users/me"), "Checking the token").get("id")
            ws_url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/api/v4/websocket"
            async with self.connect(ws_url) as socket:
                await socket.send(json.dumps({"seq": 1, "action": "authentication_challenge", "data": {"token": token}}))
                async for raw in socket:
                    event = json.loads(raw)
                    if event.get("event") != "posted":
                        continue
                    post = json.loads((event.get("data") or {}).get("post") or "{}")
                    if not post or post.get("user_id") == self.me or post.get("type"):
                        continue
                    replying_to = ""
                    if post.get("root_id"):
                        root = await api.get(f"{self.base}/api/v4/posts/{post['root_id']}")
                        root_post = root.json() if root.status_code == 200 else {}
                        if root_post.get("user_id") == self.me:
                            replying_to = root_post.get("message", "")
                    await self.hub.receive(self, Inbound(conversation=post["channel_id"], sender=post["user_id"],
                                                         text=post.get("message", ""), replying_to=replying_to))

    async def send(self, conversation: str, text: str) -> None:
        async with self.http(headers={"Authorization": f"Bearer {self.setting('token')}"}) as api:
            response = await api.post(f"{self.base}/api/v4/posts", json={"channel_id": conversation, "message": text})
            if response.status_code >= 400:
                raise ConnectorError(f"Sending to Mattermost failed ({response.status_code})")
