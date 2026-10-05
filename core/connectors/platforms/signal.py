"""Signal, through a signal-cli REST API server you run (for example the
bbernhard/signal-cli-rest-api Docker image) linked to a phone number.
Reference: Hermes gateway/platforms/signal.py (MIT), which talks to
signal-cli's JSON-RPC daemon; this uses the REST wrapper's endpoints."""
import asyncio
import base64

from core.connectors import register
from core.connectors.base import Connector, Field, Inbound, require

GROUP = "group:"


@register
class Signal(Connector):
    kind = "signal"
    label = "Signal"
    description = "A Signal number, through a signal-cli REST server you run (Docker)."
    docs_url = "https://github.com/bbernhard/signal-cli-rest-api"
    message_limit = 6000
    target_field = "default_recipient"
    sender_help = "Phone numbers allowed to message JARVIS, in +15551234567 form, one per line."
    fields = (
        Field("api_url", "signal-cli REST URL", placeholder="http://127.0.0.1:8080"),
        Field("number", "JARVIS's Signal number", placeholder="+15551234567"),
        Field("poll_seconds", "Check every (seconds)", kind="number", default="5", required=False),
        Field("default_recipient", "Notify (number)", required=False, placeholder="+15557654321"),
    )

    @property
    def base(self) -> str:
        return self.setting("api_url").rstrip("/")

    async def run(self) -> None:
        number = self.setting("number")
        interval = max(2, int(self.setting("poll_seconds", "5") or 5))
        async with self.http() as api:
            while True:
                for item in require(await api.get(f"{self.base}/v1/receive/{number}"), "Receiving from Signal") or []:
                    inbound = self._inbound(item.get("envelope") or {}, number)
                    if inbound:
                        await self.hub.receive(self, inbound)
                await asyncio.sleep(interval)

    @staticmethod
    def _inbound(envelope: dict, number: str):
        data = envelope.get("dataMessage") or {}
        if not data.get("message") and not data.get("attachments"):
            return None
        sender = envelope.get("sourceNumber") or envelope.get("source") or ""
        group = (data.get("groupInfo") or {}).get("groupId")
        quote = data.get("quote") or {}
        ours = quote.get("authorNumber", quote.get("author")) == number
        return Inbound(conversation=f"{GROUP}{group}" if group else sender, sender=sender,
                       sender_name=envelope.get("sourceName", ""), text=data.get("message") or "",
                       replying_to=quote.get("text", "") if ours else "")

    async def send(self, conversation: str, text: str) -> None:
        recipient = conversation
        if conversation.startswith(GROUP):
            group_id = conversation[len(GROUP):]
            recipient = "group." + base64.b64encode(group_id.encode()).decode()
        async with self.http() as api:
            require(await api.post(f"{self.base}/v2/send", json={"message": text, "number": self.setting("number"),
                                                                 "recipients": [recipient]}), "Sending to Signal")
