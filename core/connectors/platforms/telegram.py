"""Telegram, through the Bot API with long polling (no public URL needed).
Reference: Hermes plugins/platforms/telegram (MIT), which uses
python-telegram-bot; this talks to the same API directly. Lesson carried
over from Hermes: a wedged send must not stall receiving, so polling and
sending use separate clients, each with timeouts."""
import os
from datetime import datetime, timezone

from core.connectors import register
from core.connectors.base import Connector, ConnectorError, Field, Inbound, require

API = "https://api.telegram.org"
POLL_SECONDS = 50


@register
class Telegram(Connector):
    kind = "telegram"
    label = "Telegram"
    description = "A Telegram bot you message directly or add to a group."
    docs_url = "https://core.telegram.org/bots#how-do-i-create-a-bot"
    message_limit = 4096
    target_field = "default_chat_id"
    sender_help = "Your numeric Telegram user ID (message @userinfobot to see it), one per line."
    fields = (
        Field("bot_token", "Bot token", secret=True, help="From @BotFather.", placeholder="123456:ABC..."),
        Field("default_chat_id", "Chat for notifications", required=False,
              help="Where task results and agent notifications go: your user ID for a direct chat, or a group's ID."),
    )

    async def run(self) -> None:
        token = self.setting("bot_token")
        async with self.http(timeout=POLL_SECONDS + 20) as poll, self.http(timeout=30) as files:
            me = require(await poll.get(f"{API}/bot{token}/getMe"), "Checking the bot token")
            self.bot_id = (me.get("result") or {}).get("id")
            offset = None
            while True:
                params = {"timeout": POLL_SECONDS, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                response = await poll.get(f"{API}/bot{token}/getUpdates", params=params)
                if response.status_code == 409:
                    raise ConnectorError("this bot has a webhook set elsewhere; remove it with deleteWebhook first")
                for update in require(response, "Receiving messages").get("result") or []:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        await self.hub.receive(self, await self._inbound(message, files, token))

    async def _inbound(self, message: dict, files, token: str) -> Inbound:
        sender = message.get("from") or {}
        replied = message.get("reply_to_message") or {}
        ours = (replied.get("from") or {}).get("id") == getattr(self, "bot_id", None)
        attachments = []
        document = message.get("document")
        photo = (message.get("photo") or [None])[-1]
        for item, name in ((document, (document or {}).get("file_name")), (photo, "photo.jpg")):
            if item:
                attachments.append(await self._download(files, token, item["file_id"], name or "file"))
        return Inbound(conversation=str(message["chat"]["id"]), sender=str(sender.get("id", "")),
                       sender_name=sender.get("username") or sender.get("first_name", ""),
                       text=message.get("text") or message.get("caption") or "",
                       replying_to=(replied.get("text") or "") if ours else "",
                       attachments=[a for a in attachments if a],
                       message_id=str(message.get("message_id", "")),
                       thread_id=str(message.get("message_thread_id") or replied.get("message_id") or message.get("message_id", "")),
                       sent_at=datetime.fromtimestamp(message["date"], timezone.utc).isoformat() if message.get("date") else None,
                       source_context=replied.get("text") or "",
                       source_url=(f"https://t.me/{message['chat']['username']}/{message['message_id']}"
                                   if message["chat"].get("username") and message["chat"].get("type") in ("supergroup", "channel") else None))

    async def _download(self, client, token: str, file_id: str, name: str):
        info = require(await client.get(f"{API}/bot{token}/getFile", params={"file_id": file_id}), "Fetching a file")
        path = (info.get("result") or {}).get("file_path")
        if not path:
            return None
        response = await client.get(f"{API}/file/bot{token}/{path}")
        return (name, response.content) if response.status_code == 200 else None

    async def send(self, conversation: str, text: str) -> None:
        async with self.http(timeout=30) as client:
            require(await client.post(f"{API}/bot{self.setting('bot_token')}/sendMessage",
                                      json={"chat_id": conversation, "text": text}), "Sending to Telegram")

    async def send_file(self, conversation: str, path: str) -> bool:
        async with self.http(timeout=120) as client:
            with open(path, "rb") as handle:
                require(await client.post(f"{API}/bot{self.setting('bot_token')}/sendDocument",
                                          data={"chat_id": conversation},
                                          files={"document": (os.path.basename(path), handle)}), "Sending a file")
        return True
