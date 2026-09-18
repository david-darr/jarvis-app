"""OpenAI-compatible worker: any hosted API or local server that speaks
/chat/completions with real function calling.

Three behaviours of `core/providers/openai_compatible.py` are deliberately not
reproduced here, because each is right for a chat and wrong for an agent that
takes actions:

- it retries without tools when a server rejects the `tools` field, which
  silently turns an action-taking worker into a talker;
- it rescues tool-shaped prose into a real call, which lets a model's plain
  text execute something nobody asked for;
- it returns the last message after the round limit, which reads as an answer
  when it is really an unfinished step.

Here each of those is an explicit outcome instead. Ordinary Chats keep today's
behaviour; nothing in this file changes that path.
"""
import asyncio
import json
import uuid

import httpx

from . import MAX_TOOL_ROUNDS, role_prompt, task_prompt
from ..models import EventKind, WorkerEvent
from ..tools import ToolRejected

REQUEST_TIMEOUT_SECONDS = 180
MAX_OUTPUT_TOKENS = 2000


class UnsupportedEndpoint(RuntimeError):
    """The connection cannot do structured tool calls, so it cannot act."""


class OpenAIWorker:
    def __init__(self, context):
        self.context = context
        self.client = None
        self.cancelled = False

    async def events(self, assignment):
        service = self.context.tool_service.bind(assignment)
        endpoint = self.context.endpoint
        base_url = (endpoint.get("base_url") or "").rstrip("/")
        if not base_url:
            raise UnsupportedEndpoint("This connection has no base URL.")
        messages = [
            {"role": "system", "content": role_prompt(self.context)},
            {"role": "user", "content": task_prompt(assignment.objective)},
        ]
        schemas = service.schemas()
        usage_reported = False
        outcome = None
        headers = {"Content-Type": "application/json"}
        if endpoint.get("api_key"):
            headers["Authorization"] = f"Bearer {endpoint['api_key']}"
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            for round_number in range(MAX_TOOL_ROUNDS):
                if self.cancelled:
                    break
                body = {
                    "model": self.context.model or endpoint.get("model"),
                    "messages": messages,
                    "tools": schemas,
                    # A bounded step is a condition of admission, so the cap is
                    # sent on every request rather than trusted to the server.
                    "max_tokens": MAX_OUTPUT_TOKENS,
                }
                if endpoint.get("num_ctx"):
                    body["num_ctx"] = endpoint["num_ctx"]
                response = await self.client.post(f"{base_url}/chat/completions", headers=headers, json=body)
                if response.status_code in (400, 422):
                    raise UnsupportedEndpoint(
                        "This connection rejected structured tool calls, so it cannot take actions. "
                        "Give this agent a connection that supports function calling.")
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage") or {}
                units = _total_units(usage)
                if units:
                    usage_reported = True
                    # Every call is charged, not just the last one: keeping only
                    # the newest callback is what makes ExternalBrain's
                    # `last_usage` under-report a multi-call turn.
                    yield WorkerEvent(EventKind.USAGE, f"{assignment.attempt_id}:{round_number}", {"units": units})
                choice = (data.get("choices") or [{}])[0].get("message") or {}
                text = (choice.get("content") or "").strip()
                if text:
                    yield WorkerEvent(EventKind.TEXT, uuid.uuid4().hex, {"text": text})
                calls = choice.get("tool_calls") or []
                if not calls:
                    # Prose is never promoted into an action here. An endpoint
                    # that answers in text has simply not acted yet.
                    messages.append({"role": "assistant", "content": choice.get("content") or ""})
                    messages.append({"role": "user", "content":
                                     "That was not a tool call. Every action must be a tool call. "
                                     "Finish with submit_result or report_blocker."})
                    continue
                messages.append(choice)
                for index, call in enumerate(calls):
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    arguments = _arguments(function.get("arguments"))
                    # Found in testing: a server may reuse the same tool-call id
                    # in a later round. Action ids are unique per attempt, so
                    # position is what makes this identifier trustworthy -
                    # without it a second call collides with the first and the
                    # store correctly rejects it as a changed replay.
                    action_id = f"{round_number}-{index}-{call.get('id') or uuid.uuid4().hex}"
                    yield WorkerEvent(EventKind.ACTION_STARTED, f"{action_id}:start",
                                      {"action_id": action_id,
                                       "intent": {"tool": name, "arguments": _clip(arguments)}})
                    try:
                        result_text = service.call(name, arguments)
                        recorded = {"ok": True, "text": result_text}
                    except ToolRejected as rejected:
                        result_text = f"Rejected: {rejected}"
                        recorded = {"ok": False, "text": result_text}
                    yield WorkerEvent(EventKind.ACTION_FINISHED, f"{action_id}:end",
                                      {"action_id": action_id, "result": recorded})
                    messages.append({"role": "tool", "tool_call_id": call.get("id") or action_id,
                                     "content": result_text})
                if service.terminal is not None:
                    break
            else:
                outcome = {"status": "incomplete",
                           "reason": f"The step reached its {MAX_TOOL_ROUNDS}-round limit without finishing."}
            yield WorkerEvent(EventKind.RESULT, f"{assignment.attempt_id}:final", {
                "result": service.terminal or outcome or {
                    "status": "incomplete", "reason": "The step ended without a result or a blocker."},
                "usage_complete": usage_reported,
            })
        finally:
            client, self.client = self.client, None
            if client is not None:
                await client.aclose()

    async def cancel(self) -> bool:
        """Closing the transport ends this worker; there is nothing else running.

        An HTTP worker owns no subprocess and no server-side session, so once
        the connection is closed nothing of ours is still executing. Tokens the
        provider already produced may still be billed, which is why the store
        keeps a conservative hold until usage is settled rather than assuming a
        cancelled call cost nothing.
        """
        self.cancelled = True
        client, self.client = self.client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                return False
        return True


def _arguments(raw):
    """Ollama returns an object where the spec says JSON-encoded string."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _total_units(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    if isinstance(usage.get("total_tokens"), int):
        return usage["total_tokens"]
    return int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)


def _clip(value, limit=2000):
    text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= limit else text[:limit] + "…"
