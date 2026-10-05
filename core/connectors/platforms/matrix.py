"""Matrix, through the client-server API (long-polling /sync) with an
access token. Joins rooms it is invited to by an allowed user. Reference:
Hermes plugins/platforms/matrix (MIT), which uses matrix-nio; unencrypted
rooms only, as there."""
import uuid
from urllib.parse import quote

from core.connectors import register
from core.connectors.base import Connector, Field, Inbound, require

SYNC_TIMEOUT_MS = 30000


@register
class Matrix(Connector):
    kind = "matrix"
    label = "Matrix"
    description = "A Matrix account (Element and others). Unencrypted rooms and direct chats."
    docs_url = "https://spec.matrix.org/latest/client-server-api/"
    message_limit = 16000
    target_field = "default_room"
    sender_help = "Matrix user IDs like @david:matrix.org, one per line."
    fields = (
        Field("homeserver", "Homeserver URL", placeholder="https://matrix.org"),
        Field("user_id", "Bot user ID", placeholder="@jarvis:matrix.org"),
        Field("access_token", "Access token", secret=True, help="Element: Settings > Help & About > Access Token."),
        Field("default_room", "Room ID for notifications", required=False, placeholder="!abc123:matrix.org"),
    )

    @property
    def base(self) -> str:
        return self.setting("homeserver").rstrip("/") + "/_matrix/client/v3"

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.setting('access_token')}"}

    async def run(self) -> None:
        me = self.setting("user_id")
        allowed = set(self.record.get("allowed_senders") or [])
        async with self.http(headers=self._auth(), timeout=SYNC_TIMEOUT_MS / 1000 + 30) as api:
            since = require(await api.get(f"{self.base}/sync", params={"timeout": 0}), "First sync").get("next_batch")
            while True:
                data = require(await api.get(f"{self.base}/sync", params={"since": since, "timeout": SYNC_TIMEOUT_MS}),
                               "Receiving messages")
                since = data.get("next_batch", since)
                rooms = data.get("rooms") or {}
                for room_id, invite in (rooms.get("invite") or {}).items():
                    inviters = {e.get("sender") for e in (invite.get("invite_state") or {}).get("events", [])}
                    if inviters & allowed:
                        await api.post(f"{self.base}/join/{quote(room_id)}", json={})
                for room_id, room in (rooms.get("join") or {}).items():
                    for event in (room.get("timeline") or {}).get("events", []):
                        if event.get("type") != "m.room.message" or event.get("sender") == me:
                            continue
                        content = event.get("content") or {}
                        replying_to = await self._replied_text(api, room_id, content, me)
                        await self.hub.receive(self, Inbound(conversation=room_id, sender=event["sender"],
                                                             text=content.get("body", ""), replying_to=replying_to))

    async def _replied_text(self, api, room_id: str, content: dict, me: str) -> str:
        event_id = ((content.get("m.relates_to") or {}).get("m.in_reply_to") or {}).get("event_id")
        if not event_id:
            return ""
        response = await api.get(f"{self.base}/rooms/{quote(room_id)}/event/{quote(event_id)}")
        event = response.json() if response.status_code == 200 else {}
        return (event.get("content") or {}).get("body", "") if event.get("sender") == me else ""

    async def send(self, conversation: str, text: str) -> None:
        async with self.http(headers=self._auth()) as api:
            require(await api.put(f"{self.base}/rooms/{quote(conversation)}/send/m.room.message/{uuid.uuid4().hex}",
                                  json={"msgtype": "m.text", "body": text}), "Sending to Matrix")
