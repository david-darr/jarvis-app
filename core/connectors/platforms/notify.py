"""Send-only connectors: places JARVIS posts task results and agent
notifications to, through an incoming-webhook URL or a push service. No
bot app to set up, and nothing to receive. References: Hermes
plugins/platforms/ntfy, teams, google_chat, feishu, dingtalk, wecom (MIT)."""
import base64
import hashlib
import hmac
import time
from urllib.parse import quote_plus

from core.connectors import register
from core.connectors.base import Connector, ConnectorError, Field, require

WEBHOOK_HELP = "Treat it like a password: anyone with this URL can post here."


class _SendOnly(Connector):
    two_way = False
    sender_help = ""

    @property
    def default_target(self) -> str:
        return "default"


@register
class Ntfy(_SendOnly):
    kind = "ntfy"
    label = "ntfy (push notifications)"
    description = "Push notifications to your phone through ntfy.sh or your own ntfy server."
    docs_url = "https://docs.ntfy.sh/"
    message_limit = 3500
    fields = (
        Field("server", "Server", default="https://ntfy.sh", required=False),
        Field("topic", "Topic", help="Pick something hard to guess: anyone who knows a public topic can read it."),
        Field("token", "Access token", secret=True, required=False, help="Only for a protected topic."),
    )

    async def send(self, conversation: str, text: str) -> None:
        headers = {"Title": "JARVIS"}
        if self.setting("token"):
            headers["Authorization"] = f"Bearer {self.setting('token')}"
        async with self.http(headers=headers) as api:
            require(await api.post(f"{self.setting('server', 'https://ntfy.sh').rstrip('/')}/{self.setting('topic')}",
                                   content=text.encode("utf-8")), "Publishing to ntfy")


@register
class TeamsWebhook(_SendOnly):
    kind = "teams"
    label = "Microsoft Teams (post to a channel)"
    description = "Posts to a Teams channel through a Workflows \"post to a channel when a webhook request is received\" URL."
    docs_url = "https://support.microsoft.com/office/create-incoming-webhooks-with-workflows-for-microsoft-teams-8ae491c7-0394-4861-ba59-055e33f75498"
    message_limit = 20000
    fields = (Field("webhook_url", "Workflow webhook URL", secret=True, help=WEBHOOK_HELP),)

    async def send(self, conversation: str, text: str) -> None:
        card = {"type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": text, "wrap": True}]}
        async with self.http() as api:
            require(await api.post(self.setting("webhook_url"), json={
                "type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                    "content": card}]}), "Posting to Teams")


@register
class GoogleChatWebhook(_SendOnly):
    kind = "google_chat"
    label = "Google Chat (post to a space)"
    description = "Posts to a Google Chat space through its incoming webhook."
    docs_url = "https://developers.google.com/workspace/chat/quickstart/webhooks"
    message_limit = 4000
    fields = (Field("webhook_url", "Space webhook URL", secret=True, help=WEBHOOK_HELP),)

    async def send(self, conversation: str, text: str) -> None:
        async with self.http() as api:
            require(await api.post(self.setting("webhook_url"), json={"text": text}), "Posting to Google Chat")


@register
class FeishuWebhook(_SendOnly):
    kind = "feishu"
    label = "Feishu / Lark (group bot)"
    description = "Posts to a Feishu or Lark group through a custom bot webhook."
    docs_url = "https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot"
    message_limit = 4000
    fields = (Field("webhook_url", "Bot webhook URL", secret=True, help=WEBHOOK_HELP),
              Field("signing_secret", "Signing secret", secret=True, required=False,
                    help="Only if the bot has signature verification on."))

    async def send(self, conversation: str, text: str) -> None:
        body = {"msg_type": "text", "content": {"text": text}}
        secret = self.setting("signing_secret")
        if secret:
            timestamp = str(int(time.time()))
            sign = base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
            body.update({"timestamp": timestamp, "sign": sign})
        async with self.http() as api:
            data = require(await api.post(self.setting("webhook_url"), json=body), "Posting to Feishu")
            if data.get("code", 0) != 0:
                raise ConnectorError(f"Posting to Feishu failed: {data.get('msg', data.get('code'))}")


@register
class DingTalkWebhook(_SendOnly):
    kind = "dingtalk"
    label = "DingTalk (group bot)"
    description = "Posts to a DingTalk group through a custom robot webhook."
    docs_url = "https://open.dingtalk.com/document/robots/custom-robot-access"
    message_limit = 4000
    fields = (Field("webhook_url", "Robot webhook URL", secret=True, help=WEBHOOK_HELP),
              Field("signing_secret", "Signing secret", secret=True, required=False))

    async def send(self, conversation: str, text: str) -> None:
        url = self.setting("webhook_url")
        secret = self.setting("signing_secret")
        if secret:
            timestamp = str(int(time.time() * 1000))
            sign = base64.b64encode(hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest())
            url += ("&" if "?" in url else "?") + f"timestamp={timestamp}&sign={quote_plus(sign.decode())}"
        async with self.http() as api:
            data = require(await api.post(url, json={"msgtype": "text", "text": {"content": text}}), "Posting to DingTalk")
            if data.get("errcode", 0) != 0:
                raise ConnectorError(f"Posting to DingTalk failed: {data.get('errmsg')}")


@register
class WeComWebhook(_SendOnly):
    kind = "wecom"
    label = "WeCom (group bot)"
    description = "Posts to a WeCom (WeChat Work) group through a group robot webhook."
    docs_url = "https://developer.work.weixin.qq.com/document/path/91770"
    message_limit = 2000
    fields = (Field("webhook_url", "Robot webhook URL", secret=True, help=WEBHOOK_HELP),)

    async def send(self, conversation: str, text: str) -> None:
        async with self.http() as api:
            data = require(await api.post(self.setting("webhook_url"), json={"msgtype": "text", "text": {"content": text}}),
                           "Posting to WeCom")
            if data.get("errcode", 0) != 0:
                raise ConnectorError(f"Posting to WeCom failed: {data.get('errmsg')}")
