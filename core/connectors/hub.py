"""Runs the configured connectors and decides what every incoming message
means, the same way for every platform (see core/connectors/base.py).

The rules match Discord's (core/channels/discord_channel.py):
- A message from anyone not on the connector's allow list is ignored,
  unless the connector was explicitly opened to anyone.
- Allow-listed senders on a connector that is not open may address agents
  (core/channels/agent_routing.py): talk to one, give it a job, or reply to
  its notification. Agents run in Auto, so nobody else ever can.
- Everything else is an everyday chat, one per conversation, non-admin -
  the same as a Discord channel's chat.
"""
import asyncio
import logging
import time
from typing import Optional

from core import attachments, events
from core.channels import agent_routing
from core.connectors import store
from core.connectors.base import Connector, ConnectorError, Inbound

logger = logging.getLogger(__name__)

RESTART_BACKOFF = (5, 15, 60, 300)


def chunk(text: str, limit: int) -> list[str]:
    """Split on paragraph, then line, then hard boundaries."""
    text = text or ""
    pieces = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip():
        pieces.append(text)
    return pieces


class Hub:
    def __init__(self, kinds: dict, transport=None):
        self.kinds = kinds
        self.transport = transport  # tests: one mock transport for every adapter
        self.adapters: dict[str, Connector] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.status: dict[str, dict] = {}
        self._background: set = set()

    # -- lifecycle -----------------------------------------------------------------

    def build(self, record: dict) -> Connector:
        cls = self.kinds.get(record["kind"])
        if cls is None:
            raise ConnectorError(f"unknown platform: {record['kind']}")
        return cls(record, store.secrets_of(record), self, self.transport)

    async def start_all(self) -> None:
        for record in store.list_records():
            if record.get("enabled"):
                await self.start(record["id"])

    async def start(self, connector_id: str) -> None:
        await self.stop(connector_id)
        record = store.get_record(connector_id)
        if record is None or not record.get("enabled"):
            self._set(connector_id, "off")
            return
        try:
            adapter = self.build(record)
        except Exception as e:  # a bad saved setting must not stop the app
            self._set(connector_id, "error", str(e))
            return
        self.adapters[connector_id] = adapter
        self.tasks[connector_id] = asyncio.get_running_loop().create_task(self._supervise(adapter))

    async def stop(self, connector_id: str) -> None:
        task = self.tasks.pop(connector_id, None)
        self.adapters.pop(connector_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._set(connector_id, "off")

    async def stop_all(self) -> None:
        for connector_id in list(self.tasks):
            await self.stop(connector_id)

    async def _supervise(self, adapter: Connector) -> None:
        """Run the receive loop; on failure, record why and try again later
        rather than giving up or spinning."""
        connector_id, failures = adapter.record["id"], 0
        while True:
            self._set(connector_id, "connected" if not adapter.two_way or adapter.webhook else "listening")
            try:
                await adapter.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                delay = RESTART_BACKOFF[min(failures, len(RESTART_BACKOFF)) - 1]
                self._set(connector_id, "error", f"{e} (retrying in {delay}s)")
                logger.warning("connector %s (%s) stopped: %s", adapter.record["name"], adapter.kind, e)
                await asyncio.sleep(delay)

    def _set(self, connector_id: str, state: str, detail: str = "") -> None:
        current = self.status.get(connector_id, {})
        self.status[connector_id] = {**current, "state": state, "detail": detail, "since": time.time()}

    # -- messages ------------------------------------------------------------------

    async def receive(self, adapter: Connector, inbound: Inbound) -> None:
        record = adapter.record
        allowed = set(record.get("allowed_senders") or [])
        open_to_anyone = bool(record.get("open"))
        if inbound.sender not in allowed and not open_to_anyone:
            logger.info("connector %s: ignored a message from %s (not on the allow list)", record["name"], inbound.sender)
            return
        self.status.setdefault(record["id"], {})["last_message_at"] = time.time()
        text = (inbound.text or "").strip()
        if not text and not inbound.attachments:
            return
        from dataclasses import asdict
        from core import tab_hooks
        tab_hooks.emit_message({"kind": "connector", "connection_id": record["id"]}, asdict(inbound))
        trusted = inbound.sender in allowed and not open_to_anyone
        try:
            reply = await self._reply(adapter, inbound, text, trusted)
        except Exception:
            logger.exception("connector %s failed to handle a message", record["name"])
            reply = "Something went wrong on my end handling that. Check the app logs."
        if reply:
            await self.send_reply(adapter, inbound.conversation, reply)

    async def _reply(self, adapter: Connector, inbound: Inbound, text: str, trusted: bool) -> Optional[str]:
        record = adapter.record
        if trusted and inbound.replying_to and text:
            answered = agent_routing.answer_reply(inbound.replying_to, text)
            if answered is not None:
                return answered
        ids = self._stage(inbound)
        addressed = agent_routing.match_agent(text) if trusted else None
        if addressed:
            agent, rest = addressed
            return await agent_routing.direct(agent, rest, f"conn:{record['id']}", adapter.label, ids)
        from core.session_manager import session_manager
        from services import chat_service
        session_id = session_manager.get_or_create_channel_session(
            f"conn:{record['id']}:{inbound.conversation}", f"{record['name']} ({adapter.label})",
            model_endpoint_id=record.get("model_endpoint_id"))
        return await chat_service.send_message(session_id, text, ids)

    @staticmethod
    def _stage(inbound: Inbound) -> list[str]:
        ids = []
        for filename, content in inbound.attachments:
            try:
                ids.append(attachments.stage_file(filename, content)["id"])
            except Exception:
                logger.exception("connector: could not stage attachment %s", filename)
        return ids

    async def send_reply(self, adapter: Connector, conversation: str, reply: str) -> None:
        """Text in the platform's size limit; files Kairos generated are
        uploaded where the platform allows it, else named."""
        from core.channels.discord_channel import _extract_generated_attachments
        text, paths = _extract_generated_attachments(reply)
        for path in paths:
            try:
                if await adapter.send_file(conversation, path):
                    continue
            except Exception:
                logger.exception("connector %s could not upload %s", adapter.record["name"], path)
            import os
            text += f"\n(Saved in Kairos: {os.path.basename(path)})"
        for piece in chunk(text, adapter.message_limit):
            await adapter.send(conversation, piece)

    async def deliver(self, connector_id: str, text: str) -> bool:
        """Task output and agent notifications to the connector's default
        destination. False, never an exception, on any failure."""
        adapter = self.adapters.get(connector_id)
        if adapter is None or not adapter.default_target:
            return False
        try:
            await self.send_reply(adapter, adapter.default_target, text)
            return True
        except Exception as e:
            logger.warning("connector %s: delivery failed: %s", adapter.record["name"], e)
            events.emit("connector.delivery_failed", f"{adapter.record['name']}: delivery failed: {e}", level="error")
            return False

    def spawn(self, adapter: Connector, inbound: Inbound) -> None:
        """Handle a webhook's message after the webhook has been answered:
        platforms give up on a webhook within seconds, a model turn takes
        longer."""
        task = asyncio.get_running_loop().create_task(self.receive(adapter, inbound))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def webhook(self, connector_id: str, method: str, headers: dict, query: dict, body: bytes):
        adapter = self.adapters.get(connector_id)
        if adapter is None or not adapter.webhook:
            return 404, b"not found", "text/plain"
        return await adapter.handle_webhook(method, headers, query, body)
