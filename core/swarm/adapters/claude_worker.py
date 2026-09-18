"""Claude Agent SDK worker, scoped to Swarm's own tools.

This deliberately does not reuse `core/brain.py`. That class grants the app's
own source directories, the hive-mind vault tools, `acceptEdits`, and Bash for
an admin - correct for the owner's chat, wrong for an unattended worker. Here
the only tools that exist are Swarm's, built as an in-process MCP server, and
every built-in file, shell and web tool is denied by name rather than merely
left off the pre-approval list, so an attempt fails closed instead of hanging
on a permission prompt nothing can answer headlessly.

Ordering matters more than it looks: the SDK executes a tool inside its own
message loop, so the tool handler hands its intent to this generator and waits
until the runtime has actually persisted it before doing anything. That is
what makes "intent recorded before the side effect" true rather than hopeful.
"""
import asyncio
import uuid

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from . import MAX_TURNS, QUOTA_FRESHNESS_SECONDS, role_prompt, task_prompt
from ..models import EventKind, WorkerEvent
from ..tools import ToolRejected

SERVER_NAME = "swarm"
# Every built-in surface that would reach outside the scratch directory. Denied
# by name: omitting a tool only leaves it unapproved, which hangs a headless
# session on a prompt instead of failing.
DENIED_TOOLS = [
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "SlashCommand", "ExitPlanMode",
]
MESSAGE_TIMEOUT_SECONDS = 180
INTERRUPT_TIMEOUT_SECONDS = 5
SETTLE_TIMEOUT_SECONDS = 5
_DONE = object()


class ClaudeWorker:
    def __init__(self, context):
        self.context = context
        self.client = None
        self.consumer = None
        self.interrupted = False
        self.interrupt_confirmed = False
        self.produced_result = False

    def _server(self, queue):
        service = self.context.tool_service

        def make(name, definition):
            # The whole JSON Schema, not just its properties map. Found live
            # 2026-09-16: passing only the properties made the SDK read each
            # nested schema as a type hint, so an array parameter reached the
            # model as a string and it dutifully sent stringified JSON, three
            # times, before giving up.
            @tool(name, definition["description"], definition["parameters"])
            async def handler(arguments: dict) -> dict:
                action_id = uuid.uuid4().hex
                await _hand_off(queue, WorkerEvent(
                    EventKind.ACTION_STARTED, f"{action_id}:start",
                    {"action_id": action_id, "intent": {"tool": name, "arguments": _clip(arguments)}}))
                try:
                    text = service.call(name, arguments or {})
                    outcome = {"ok": True, "text": text}
                except ToolRejected as rejected:
                    text = f"Rejected: {rejected}"
                    outcome = {"ok": False, "text": text}
                await _hand_off(queue, WorkerEvent(
                    EventKind.ACTION_FINISHED, f"{action_id}:end",
                    {"action_id": action_id, "result": outcome}))
                return {"content": [{"type": "text", "text": text}]}

            return handler

        return create_sdk_mcp_server(
            name=SERVER_NAME, version="1.0.0",
            tools=[make(name, definition) for name, definition in service.definitions.items()],
        )

    def _options(self, queue):
        service = self.context.tool_service
        allowed = [f"mcp__{SERVER_NAME}__{name}" for name in service.definitions]
        return ClaudeAgentOptions(
            cwd=self.context.scratch_dir,
            # A bare string, not the claude_code preset: a Swarm worker is not
            # the owner's assistant and must not inherit that persona's
            # conventions about editing files it does not have.
            system_prompt=role_prompt(self.context),
            mcp_servers={SERVER_NAME: self._server(queue)},
            strict_mcp_config=True,
            allowed_tools=allowed,
            disallowed_tools=DENIED_TOOLS,
            # No user or project settings, so a personal CLAUDE.md or local
            # permission file cannot widen an unattended worker.
            setting_sources=[],
            permission_mode="default",
            max_turns=MAX_TURNS,
            model=self.context.model,
            effort=self.context.effort,
            env=dict(self.context.env),
            include_partial_messages=False,
        )

    async def events(self, assignment):
        self.context.tool_service.bind(assignment)
        queue: asyncio.Queue = asyncio.Queue()
        self.consumer = asyncio.create_task(self._consume(assignment, queue))
        finished = False
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    finished = True
                    break
                event, acknowledged = item
                yield event
                if acknowledged is not None:
                    acknowledged.set()
        finally:
            # Found on the first live run: only an early exit needs a cancel.
            # A consumer that has already published _DONE is unwinding its own
            # connection, and cancelling it mid-disconnect turned a clean
            # finish into a CancelledError - which the runtime then read as an
            # unconfirmed stop and recorded as an unknown attempt.
            if not finished and self.consumer and not self.consumer.done():
                self.consumer.cancel()
        if not finished:
            return
        try:
            await self.consumer
        except asyncio.CancelledError:
            pass
        except Exception:
            # Noise while closing a connection whose result is already
            # recorded must not fail a finished step; a failure before any
            # result was produced still surfaces, with its own message.
            if not self.produced_result:
                raise

    async def _consume(self, assignment, queue):
        service = self.context.tool_service
        usage_reported = False
        try:
            self.client = ClaudeSDKClient(options=self._options(queue))
            await self.client.connect()
            await self.client.query(task_prompt(assignment.objective))
            responses = self.client.receive_response().__aiter__()
            while True:
                try:
                    message = await asyncio.wait_for(responses.__anext__(), timeout=MESSAGE_TIMEOUT_SECONDS)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise RuntimeError("Claude stopped responding")
                if isinstance(message, AssistantMessage):
                    text = "\n\n".join(block.text for block in message.content if isinstance(block, TextBlock))
                    if text.strip():
                        await _hand_off(queue, WorkerEvent(EventKind.TEXT, uuid.uuid4().hex, {"text": text}))
                elif isinstance(message, ResultMessage):
                    total = _total_units(message.usage)
                    if total:
                        usage_reported = True
                        await _hand_off(queue, WorkerEvent(EventKind.USAGE, f"{assignment.attempt_id}:result",
                                                           {"units": total}))
                    if message.is_error and service.terminal is None:
                        raise RuntimeError("Claude reported an unsuccessful turn")
                    break
                elif type(message).__name__ == "RateLimitEvent":
                    quota = _quota_event(message)
                    if quota:
                        await _hand_off(queue, quota)
                if service.terminal is not None:
                    # The step is over; stop paying for further turns.
                    await self._interrupt()
            self.produced_result = True
            await queue.put((WorkerEvent(EventKind.RESULT, f"{assignment.attempt_id}:final", {
                "result": service.terminal or {"status": "incomplete",
                                               "reason": "The step ended without submitting a result or a blocker."},
                "usage_complete": usage_reported,
            }), None))
        finally:
            await queue.put(_DONE)
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass

    async def _interrupt(self):
        """Ask the CLI to stop, under a bound.

        Measured live, 2026-09-16: `interrupt()` does not return while the CLI
        is mid-turn - not at 5 seconds and not at 25. Unbounded, it ran past
        the runtime's own stop budget and `cancel()` never answered at all.
        Bounded, a pause is fast and the outcome is honest: the interrupt goes
        unacknowledged, so the attempt is recorded as unknown and its task is
        blocked for reconciliation rather than being called a clean stop.
        """
        if self.interrupted or self.client is None:
            return self.interrupt_confirmed
        self.interrupted = True
        try:
            await asyncio.wait_for(self.client.interrupt(), timeout=INTERRUPT_TIMEOUT_SECONDS)
            self.interrupt_confirmed = True
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            self.interrupt_confirmed = False
        return self.interrupt_confirmed

    async def cancel(self) -> bool:
        """True only when this worker is genuinely finished.

        An interrupt that raises, or a consumer task that will not end, is
        reported as an unconfirmed stop so the attempt stays unknown rather
        than being recorded as a clean cancellation.
        """
        acknowledged = await self._interrupt()
        if self.consumer is None:
            return True
        if not self.consumer.done():
            self.consumer.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(_settled(self.consumer)), timeout=SETTLE_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        # The local task is finished either way. Only an acknowledged
        # interrupt is evidence the CLI itself stopped, so an unacknowledged
        # one stays unconfirmed and the attempt is recorded as unknown.
        return bool(acknowledged)


async def _settled(task):
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _hand_off(queue, event):
    """Publish an event and wait until the runtime has recorded it.

    The runtime acknowledges by advancing the iterator, so returning from here
    means the intent is durable. A tool must never act before that.
    """
    acknowledged = asyncio.Event()
    await queue.put((event, acknowledged))
    await acknowledged.wait()


def _clip(value, limit=2000):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _total_units(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    if isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"]
    # input/output are the non-overlapping totals; cache fields are subsets of
    # input, so summing every *_tokens key would double count.
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def _quota_event(message):
    """Turn the SDK's rate-limit message into a pool-level quota reading.

    Utilization is reported as a fraction; the store stores a percentage.
    Absence of a number is recorded as unknown, never as zero.
    """
    info = getattr(message, "rate_limit_info", None)
    if info is None:
        return None
    status = {"allowed": "allowed", "allowed_warning": "warning", "rejected": "rejected"}.get(
        getattr(info, "status", None), "unknown")
    utilization = getattr(info, "utilization", None)
    percent = None
    if isinstance(utilization, (int, float)):
        percent = float(utilization) * 100 if utilization <= 1 else float(utilization)
        percent = max(0.0, min(100.0, percent))
    return WorkerEvent(EventKind.QUOTA, uuid.uuid4().hex, {
        "bucket": getattr(info, "rate_limit_type", None) or "account",
        "used_percent": percent,
        "status": status,
        "resets_at": getattr(info, "resets_at", None),
        "freshness": QUOTA_FRESHNESS_SECONDS,
    })
