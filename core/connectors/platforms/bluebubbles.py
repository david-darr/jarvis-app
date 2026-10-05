"""iMessage, through a BlueBubbles server running on a Mac you own.
Reference: Hermes gateway/platforms/bluebubbles.py (MIT), which takes
BlueBubbles webhooks; this polls the server's REST API instead, so it works
without exposing JARVIS to the internet."""
import asyncio
import time
import uuid

from core.connectors import register
from core.connectors.base import Connector, Field, Inbound, require


@register
class BlueBubbles(Connector):
    kind = "bluebubbles"
    label = "iMessage (BlueBubbles)"
    description = "iMessage through a BlueBubbles server on a Mac."
    docs_url = "https://bluebubbles.app/"
    message_limit = 10000
    target_field = "default_chat"
    sender_help = "Phone numbers or Apple ID emails allowed to message JARVIS, one per line."
    fields = (
        Field("server_url", "BlueBubbles server URL", placeholder="http://192.168.1.20:1234"),
        Field("password", "Server password", secret=True),
        Field("poll_seconds", "Check every (seconds)", kind="number", default="5", required=False),
        Field("default_chat", "Notify (chat GUID)", required=False, placeholder="iMessage;-;+15551234567"),
    )

    @property
    def base(self) -> str:
        return self.setting("server_url").rstrip("/") + "/api/v1"

    def _params(self) -> dict:
        return {"password": self.setting("password")}

    async def run(self) -> None:
        interval = max(2, int(self.setting("poll_seconds", "5") or 5))
        after = int(time.time() * 1000)
        async with self.http() as api:
            while True:
                found = require(await api.post(f"{self.base}/message/query", params=self._params(),
                                               json={"limit": 50, "sort": "ASC", "after": after,
                                                     "with": ["chat", "handle"]}), "Receiving iMessages")
                for message in found.get("data") or []:
                    after = max(after, int(message.get("dateCreated") or after) + 1)
                    if message.get("isFromMe") or not message.get("text"):
                        continue
                    chat = (message.get("chats") or [{}])[0].get("guid", "")
                    sender = (message.get("handle") or {}).get("address", "")
                    replying_to = await self._replied(api, message.get("threadOriginatorGuid"))
                    await self.hub.receive(self, Inbound(conversation=chat, sender=sender, text=message["text"],
                                                         replying_to=replying_to))
                await asyncio.sleep(interval)

    async def _replied(self, api, guid) -> str:
        if not guid:
            return ""
        response = await api.get(f"{self.base}/message/{guid}", params=self._params())
        original = (response.json() if response.status_code == 200 else {}).get("data") or {}
        return original.get("text", "") if original.get("isFromMe") else ""

    async def send(self, conversation: str, text: str) -> None:
        async with self.http() as api:
            require(await api.post(f"{self.base}/message/text", params=self._params(),
                                   json={"chatGuid": conversation, "tempGuid": uuid.uuid4().hex, "message": text}),
                    "Sending an iMessage")
