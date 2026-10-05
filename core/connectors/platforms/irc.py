"""IRC, over a plain asyncio TLS socket. Reference: Hermes
plugins/platforms/irc (MIT). In a channel JARVIS answers only messages
addressed to its nick ("jarvis: ..."); private messages always.

Nicknames are only trustworthy on a network where they are registered, so
the allow list is only as strong as the network's NickServ."""
import asyncio
import ssl

from core.connectors import register
from core.connectors.base import Connector, ConnectorError, Field, Inbound

LINE_LIMIT = 400


@register
class IRC(Connector):
    kind = "irc"
    label = "IRC"
    description = "A bot on an IRC network (Libera.Chat and others)."
    docs_url = "https://libera.chat/guides/"
    message_limit = 2000
    target_field = "default_target"
    sender_help = "Nicknames allowed to talk to JARVIS (registered nicks only), one per line."
    fields = (
        Field("server", "Server", placeholder="irc.libera.chat"),
        Field("port", "Port", kind="number", default="6697", required=False),
        Field("tls", "Use TLS", kind="bool", default="true", required=False),
        Field("nick", "Nickname", placeholder="jarvis-bot"),
        Field("password", "NickServ password", secret=True, required=False),
        Field("channels", "Channels to join", required=False, placeholder="#mychannel, #other"),
        Field("default_target", "Notify (channel or nick)", required=False),
    )

    async def open(self):
        use_tls = self.setting("tls", "true").lower() not in ("false", "0", "no")
        return await asyncio.open_connection(self.setting("server"), int(self.setting("port", "6697") or 6697),
                                             ssl=ssl.create_default_context() if use_tls else None)

    async def run(self) -> None:
        reader, writer = await self.open()
        self._writer = writer
        nick = self.setting("nick")
        try:
            if self.setting("password"):
                await self._line(f"PASS {self.setting('password')}")
            await self._line(f"NICK {nick}")
            await self._line(f"USER {nick} 0 * :JARVIS")
            while True:
                raw = await reader.readline()
                if not raw:
                    raise ConnectorError("the IRC server closed the connection")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("PING"):
                    await self._line("PONG" + line[4:])
                    continue
                parts = line.split(" ", 3)
                if len(parts) >= 2 and parts[1] == "001":
                    for channel in [c.strip() for c in self.setting("channels").split(",") if c.strip()]:
                        await self._line(f"JOIN {channel}")
                elif len(parts) == 4 and parts[1] == "PRIVMSG":
                    await self._privmsg(parts, nick)
        finally:
            writer.close()
            self._writer = None

    async def _privmsg(self, parts: list, nick: str) -> None:
        sender = parts[0].lstrip(":").split("!", 1)[0]
        target, text = parts[2], parts[3][1:] if parts[3].startswith(":") else parts[3]
        if target.startswith("#"):
            prefix = next((p for p in (f"{nick}:", f"{nick},") if text.lower().startswith(p.lower())), None)
            if not prefix:
                return
            text, conversation = text[len(prefix):].strip(), target
        else:
            conversation = sender
        await self.hub.receive(self, Inbound(conversation=conversation, sender=sender, text=text))

    async def _line(self, text: str) -> None:
        writer = getattr(self, "_writer", None)
        if writer is None:
            raise ConnectorError("not connected to IRC")
        writer.write((text.replace("\r", " ").replace("\n", " ") + "\r\n").encode("utf-8"))
        await writer.drain()

    async def send(self, conversation: str, text: str) -> None:
        for line in text.splitlines():
            while line:
                await self._line(f"PRIVMSG {conversation} :{line[:LINE_LIMIT]}")
                line = line[LINE_LIMIT:]
