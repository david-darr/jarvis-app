"""The shape every connector shares (2026-10-05; vault spec "Connectors -
Hermes Platforms (Build Spec)").

A connector is one account on one messaging platform: a Telegram bot, a
Slack app, an email inbox. Each platform is a subclass of Connector that
knows only its own API: how to receive (a long-running `run()` loop, or a
webhook), and how to `send()`. Everything a message means - who may send
it, which chat it belongs to, whether it is for an agent - is decided once,
in core/connectors/hub.py, the same way for every platform.

Modelled on Hermes Agent's BasePlatformAdapter (gateway/platforms/base.py,
MIT), reduced to what Kairos needs. No platform SDKs: each adapter talks to
its service's HTTP or WebSocket API with httpx/websockets, which Kairos
already ships.
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from core.connectors.hub import Hub


@dataclass(frozen=True)
class Field:
    """One setting a platform needs, shown in Settings > Channels."""
    key: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ""
    placeholder: str = ""
    kind: str = "text"  # text | number | bool | textarea
    default: str = ""

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "secret": self.secret, "required": self.required,
                "help": self.help, "placeholder": self.placeholder, "kind": self.kind, "default": self.default}


@dataclass
class Inbound:
    """A message that arrived. `conversation` is where a reply goes (chat,
    channel, room or phone number); `sender` is the stable ID checked
    against the connector's allow list."""
    conversation: str
    sender: str
    text: str
    sender_name: str = ""
    attachments: list = field(default_factory=list)  # [(filename, bytes)]
    replying_to: str = ""  # the text of our own message this one replies to
    # Optional provenance for passive consumers such as the CRM tab.
    message_id: str = ""
    thread_id: str = ""
    sent_at: str | None = None
    source_url: str | None = None
    source_context: str = ""  # CRM context; does not change chat reply routing


class ConnectorError(Exception):
    """A failure worth showing the person as the connector's status."""


class Connector:
    kind: str = ""
    label: str = ""
    description: str = ""
    docs_url: str = ""
    fields: tuple = ()
    two_way: bool = True        # can receive messages at all
    webhook: bool = False       # receives through POST /api/connectors/<id>/webhook
    message_limit: int = 4000   # the platform's per-message text limit
    target_field: Optional[str] = None  # the setting naming where deliveries go
    sender_help: str = "Sender IDs allowed to message Kairos here, one per line."

    def __init__(self, record: dict, secrets: dict, hub: "Hub", transport: Optional[httpx.AsyncBaseTransport] = None):
        self.record = record
        self._secrets = secrets
        self.hub = hub
        self.transport = transport

    # -- settings -----------------------------------------------------------------

    def setting(self, key: str, default: str = "") -> str:
        if key in self._secrets:
            return self._secrets[key]
        value = (self.record.get("settings") or {}).get(key)
        return default if value in (None, "") else str(value)

    @property
    def default_target(self) -> str:
        return self.setting(self.target_field) if self.target_field else ""

    def http(self, **kwargs) -> httpx.AsyncClient:
        """An HTTP client for this platform's API; tests pass a mock transport."""
        kwargs.setdefault("timeout", httpx.Timeout(30.0, read=70.0))
        return httpx.AsyncClient(transport=self.transport, **kwargs)

    # -- what a platform implements ---------------------------------------------------

    async def run(self) -> None:
        """Receive until cancelled; call `await self.hub.receive(self, Inbound(...))`
        for each message. Send-only and webhook platforms wait instead."""
        import asyncio
        await asyncio.Event().wait()

    async def send(self, conversation: str, text: str) -> None:
        raise NotImplementedError

    async def send_file(self, conversation: str, path: str) -> bool:
        """Upload a file Kairos generated. False: this platform cannot, and
        the hub mentions the file by name instead."""
        return False

    async def handle_webhook(self, method: str, headers: dict, query: dict, body: bytes):
        """For webhook platforms: verify, parse, call hub.receive, and return
        (status, body, content_type)."""
        return 404, b"", "text/plain"


def require(response: httpx.Response, what: str) -> dict:
    """The JSON of a successful API call, or a ConnectorError naming what
    failed. Never includes the request (it may carry a token)."""
    if response.status_code >= 400:
        detail = response.text[:300].strip()
        raise ConnectorError(f"{what} failed ({response.status_code}){': ' + detail if detail else ''}")
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {}
