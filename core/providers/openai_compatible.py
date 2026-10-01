"""Common API tool loop. Custom and local endpoints use OpenAI-compatible
chat completions. Official Anthropic and OpenAI hosts use native Messages and
Responses transports through core/providers/native_api.py.

Real tool-calling loop added (David's ask 2026-08-31: shared memory +
cross-session awareness "out of the box for all imported AI models both
local and API") — uses the standard OpenAI `tools`/`tool_calls` function-
calling protocol, which most modern local (Ollama, LM Studio, vLLM) and
hosted OpenAI-compatible APIs support. Not every model does, though, so
this degrades gracefully: if the endpoint rejects the `tools` field outright
(some older/smaller local models 400 on an unrecognized param), it retries
once as a plain chat call with no tools rather than failing the whole turn.

num_ctx capping added 2026-09-01 (real incident — a local model loaded with
no cap defaulted to its max context window and its KV cache alone ate
~21GB of RAM). Real bug caught live before this shipped: Ollama 0.33.1's
OpenAI-*compatible* `/v1/chat/completions` silently ignores `num_ctx` in
every shape tried (nested under `options`, top-level per Ollama's own
docs — neither actually changed the loaded context size, verified directly
against a running Ollama instance). Only Ollama's *native* `/api/chat`
endpoint honors it. `_post_chat()` below is the one place that decides:
a detected local Ollama endpoint (core/ollama_client.is_ollama_url) with
num_ctx set routes through `ollama_client.chat_capped()` instead of the
generic HTTP path; everything else (non-Ollama local server, hosted API, or
no num_ctx set) is unaffected and still gets a harmless top-level
`num_ctx` field sent (ignored by servers that don't recognize it).

Fake-tool-call rescue added 2026-09-01 (real incident — a small local test
model, given the hive-mind memory tools, replied with a plain-text JSON
blob shaped exactly like a tool call — `{"name": "search_sessions",
"arguments": {...}}` — instead of using the API's real structured
`tool_calls` field). Some smaller/weaker models understand the *concept*
of calling a tool well enough to produce that shape, but aren't reliable
about actually using the dedicated field for it. `_extract_fake_tool_call()`
below detects that exact pattern (content is a JSON object, its "name" is
one of the tools actually offered this turn — not just anything JSON-
shaped) and rescues it into a real tool call so it actually executes,
instead of the user seeing raw JSON as if it were the model's answer.

Tool rounds kept, 2026-09-22 (prompt-cache audit finding 2): a caller can
pass `rounds`, a list this module appends each tool-calling assistant
message and each tool result to as they happen - the very same dicts sent
in the request. The caller keeps them in its history, so the next turn's
request extends this one exactly (the provider's prompt cache carries over)
and the model still has its earlier tool results. They used to exist only
in this module's local copy of the history and were dropped after every
turn. Appended as they happen, not at the end, so a stopped turn still
records the tools that actually ran.
"""
import json
import re
from typing import Awaitable, AsyncIterator, Callable, Optional
from urllib.parse import urlparse

import httpx

from core import ollama_client
from core.providers import native_api

TIMEOUT_SECONDS = 120
MAX_TOOL_ROUNDS = 4  # bounded so a model that keeps calling tools can't loop forever


def _headers(api_key: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _parse_tool_arguments(raw) -> dict:
    """Real bug found live, 2026-09-01: strict OpenAI spec requires
    `function.arguments` to be a JSON-*encoded string*, but Ollama's native
    tool-calling returns it as an actual dict/object directly — a real
    divergence from the spec, not a hypothetical. Handles both rather than
    assuming the spec-compliant shape and crashing on Ollama's real one."""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_fake_tool_call(content: Optional[str], tools: Optional[list[dict]]) -> Optional[dict]:
    """See module docstring. Returns an OpenAI-shaped tool_calls entry if
    `content` is a JSON object naming one of this turn's real tools, else
    None. Deliberately strict — only a known tool name matches, so a real
    answer that happens to start/end with braces (e.g. describing a JSON
    file) is never misread as a tool call."""
    if not tools or not content:
        return None
    stripped = content.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name, args = obj.get("name"), obj.get("arguments")
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    known_names = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    if name not in known_names:
        return None
    return {"id": f"rescued-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


async def _answer_without_tools(client, base_url: str, api_key: Optional[str], model: str,
                                working_messages: list[dict], num_ctx: Optional[int],
                                on_usage: Optional[Callable[[dict], None]]) -> str:
    """When a model spends every tool round calling tools, ask once more
    with tools switched off, so it answers from what it gathered. The
    streaming path used to end with no reply at all (found live 2026-09-25:
    qwen2.5-coder:32b searched four times in a row and the chat got an empty
    answer). A server that rejects the tool history without tools gets the
    old, honest note instead of an error."""
    body = {"model": model, "messages": _request_messages(base_url, model, working_messages + [
        {"role": "user", "content": "[You have used all the tool calls available for this message. "
                                    "Answer now from what you found, without calling any tools.]"}])}
    if num_ctx:
        body["num_ctx"] = num_ctx
    try:
        data = await _post_chat(client, base_url, api_key, body)
    except httpx.HTTPError:
        return "(no response after tool calls)"
    if on_usage and data.get("usage"):
        on_usage(data["usage"])
    content = data["choices"][0]["message"].get("content") or ""
    # Still trying to call something: say so plainly rather than show JSON.
    if not content.strip() or _looks_like_call(content):
        return "(no response after tool calls)"
    return content


def _looks_like_call(content: str) -> bool:
    stripped = content.strip()
    return stripped.startswith("{") and '"name"' in stripped and '"arguments"' in stripped


_INVENTED_TURN = re.compile(r"(?:```\s*)?(?:<\|im_start\|>|<tool_response>|<\|?tool|(?:user|assistant|tool|function)\s*(?:\n|:))", re.I)


def _rescue_tool_call(content: Optional[str], tools: Optional[list[dict]]) -> tuple[Optional[dict], Optional[str]]:
    """The fake-tool-call rescue, widened (found live 2026-09-25 on
    qwen2.5-coder:32b): besides a reply that is nothing but the call, accept
    a call that ENDS the reply - after a sentence, repeated, or in a ```json
    fence. Returns (call, the text before it), or (None, None).

    Still strict where it matters: the object must name one of this turn's
    real tools with an object of arguments, and nothing but more copies of
    it, fences and whitespace may follow - so an answer that merely shows
    some JSON mid-reply is left alone."""
    whole = _extract_fake_tool_call(content, tools)
    if whole or not tools or not content or len(content) > 20000:
        return whole, None
    decoder = json.JSONDecoder()
    found = []  # (start, end, call)
    at = content.find("{")
    while at != -1:
        try:
            obj, end = decoder.raw_decode(content, at)
        except json.JSONDecodeError:
            at = content.find("{", at + 1)
            continue
        call = _extract_fake_tool_call(json.dumps(obj), tools) if isinstance(obj, dict) else None
        if call:
            found.append((at, end, call))
        at = content.find("{", end)
    if not found:
        return None, None
    # A model that writes its call as text can then run on and invent the
    # next turn itself - "user", a <tool_response> with a made-up result -
    # instead of stopping (seen live on qwen2.5-coder:32b with a 4096-token
    # context). The invented part is discarded, never trusted: the real tool
    # runs instead.
    for index, (start, end, call) in enumerate(found):
        if _INVENTED_TURN.match(content[end:].lstrip()):
            content = content[:end]
            found = found[:index + 1]
            break

    def filler(text: str) -> bool:
        return not text.replace("```json", "").replace("```", "").strip()

    # Walk back from the end over calls separated only by filler.
    tail_start = None
    cursor = len(content)
    for start, end, call in reversed(found):
        if not filler(content[end:cursor]):
            break
        if found[-1][2]["function"] != call["function"]:
            break  # a different call earlier on is not a repeat of this one
        tail_start, cursor = start, start
    if tail_start is None:
        return None, None
    prose = content[:tail_start].rstrip()
    if prose.endswith("```json"):
        prose = prose[: -len("```json")].rstrip()
    elif prose.endswith("```"):
        prose = prose[:-3].rstrip()
    return found[-1][2], (prose or None)


def _without_tool_rounds(messages: list[dict]) -> list[dict]:
    """The text-only view of a history, for the no-tools fallback below: an
    endpoint that rejects the `tools` field may reject tool-role messages
    too, and a fallback that used to work must not start failing because
    the history now carries tool rounds."""
    plain = []
    for m in messages:
        if m.get("role") == "tool":
            continue
        if m.get("tool_calls"):
            if m.get("content"):
                plain.append({"role": m["role"], "content": m["content"]})
            continue
        plain.append(m)
    return plain


# Prompt-cache audit finding 5 (2026-09-22), laid out as Hermes's
# agent/prompt_caching.py does for envelope routes. OpenAI, DeepSeek and local
# servers reuse a cached prefix on their own; an Anthropic model reached through
# OpenRouter caches only where the request carries a cache_control marker, and
# JARVIS sent none, so every turn re-billed the whole conversation.
_CACHE_MARKER = {"type": "ephemeral"}
_MAX_CACHE_MARKERS = 4  # the most one Anthropic request accepts


def _needs_cache_markers(base_url: str, model: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    on_openrouter = host == "openrouter.ai" or host.endswith(".openrouter.ai")
    return on_openrouter and "claude" in (model or "").lower()


def _can_carry_marker(message: dict) -> bool:
    # Only inside a content part: OpenRouter hangs on a marker on a role:tool
    # envelope and ignores one on an empty assistant turn, so a message with no
    # text (a tool call) gets none rather than wasting one of the four.
    content = message.get("content")
    if isinstance(content, str):
        return content != ""
    return isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "text" for part in content)


def _marked(message: dict) -> dict:
    content = message["content"]
    if isinstance(content, str):
        parts = [{"type": "text", "text": content, "cache_control": dict(_CACHE_MARKER)}]
    else:
        parts = list(content)
        index = max(i for i, part in enumerate(parts) if isinstance(part, dict) and part.get("type") == "text")
        parts[index] = {**parts[index], "cache_control": dict(_CACHE_MARKER)}
    return {**message, "content": parts}


def _request_messages(base_url: str, model: str, messages: list[dict]) -> list[dict]:
    """The messages as this request sends them. Unchanged except for an
    Anthropic model on OpenRouter, which gets a marker on the system message
    (covering the tool list too, which renders before it) and on each of the
    last three messages, so every turn reads what the previous one cached.
    A copy: markers never reach the stored history, which stays
    byte-comparable from turn to turn."""
    if not _needs_cache_markers(base_url, model):
        return messages
    marked = list(messages)
    used = 0
    if marked and marked[0].get("role") == "system" and _can_carry_marker(marked[0]):
        marked[0] = _marked(marked[0])
        used = 1
    carriers = [i for i, m in enumerate(marked) if m.get("role") != "system" and _can_carry_marker(m)]
    for i in carriers[-(_MAX_CACHE_MARKERS - used):]:
        marked[i] = _marked(marked[i])
    return marked


def _record(working_messages: list[dict], rounds: Optional[list[dict]], message: dict) -> None:
    working_messages.append(message)
    if rounds is not None:
        rounds.append(message)


async def _post_chat(client: httpx.AsyncClient, base_url: str, api_key: Optional[str], body: dict) -> dict:
    """The one place num_ctx capping actually gets applied — see module
    docstring. `body` may carry a `num_ctx` key; it's popped here rather
    than left in the JSON sent to non-Ollama servers verbatim, since this
    function is the boundary that decides which transport handles it."""
    num_ctx = body.pop("num_ctx", None)
    if native_api.mode(base_url):
        return await native_api.post(client, base_url, body["model"], api_key,
                                     body["messages"], body.get("tools"))
    if num_ctx and ollama_client.is_ollama_url(base_url):
        return await ollama_client.chat_capped(
            body["model"], body["messages"], num_ctx, tools=body.get("tools"), base_url=base_url,
        )
    if num_ctx:
        body["num_ctx"] = num_ctx  # harmless best-effort for non-Ollama servers
    resp = await client.post(f"{base_url}/chat/completions", headers=_headers(api_key), json=body)
    resp.raise_for_status()
    return resp.json()


async def run_turn(base_url: str, model: str, api_key: Optional[str], messages: list[dict],
                    tools: Optional[list[dict]] = None, tool_executor: Optional[Callable[[str, dict], Awaitable[str]]] = None,
                    on_usage: Optional[Callable[[dict], None]] = None, num_ctx: Optional[int] = None,
                    rounds: Optional[list[dict]] = None) -> str:
    """Non-streaming chat completion, with an optional bounded tool-calling
    loop (David's ask 2026-08-31 — see module docstring). Without `tools`,
    behaves exactly as before this change.

    on_usage (David's ask 2026-09-01, per-model token usage on Home) fires
    with the raw `usage` object from any response that includes one —
    best-effort, since not every OpenAI-compatible server returns usage on
    every call; a server that never does simply never reports usage, same
    honest-degradation posture as the tools fallback above.

    num_ctx: see module docstring — applied by _post_chat().
    rounds: see module docstring."""
    working_messages = list(messages)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for _ in range(MAX_TOOL_ROUNDS if tools else 1):
            body = {"model": model, "messages": _request_messages(base_url, model, working_messages)}
            if tools:
                body["tools"] = tools
            if num_ctx:
                body["num_ctx"] = num_ctx
            try:
                data = await _post_chat(client, base_url, api_key, body)
            except httpx.HTTPStatusError as e:
                if tools and e.response.status_code in (400, 422):
                    # This endpoint doesn't understand `tools` at all — retry
                    # once, plain, rather than failing the turn outright.
                    retry_body = {"model": model, "messages": _request_messages(base_url, model, _without_tool_rounds(working_messages))}
                    if num_ctx:
                        retry_body["num_ctx"] = num_ctx
                    data = await _post_chat(client, base_url, api_key, retry_body)
                    if on_usage and data.get("usage"):
                        on_usage(data["usage"])
                    return data["choices"][0]["message"]["content"]
                raise

            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
            if not tool_calls and tool_executor:
                rescued, prose = _rescue_tool_call(message.get("content"), tools)
                if rescued:
                    tool_calls = [rescued]
                    # Rewrite so the appended history has a real tool_calls
                    # field instead of the raw hallucinated JSON text — some
                    # servers reject an assistant message followed by tool
                    # messages when it doesn't actually claim to have called one.
                    # Any sentence before the call stays as the message text.
                    message = {"role": "assistant", "content": prose, "tool_calls": tool_calls}
            if not tool_calls or not tool_executor:
                return message.get("content") or ""

            _record(working_messages, rounds, message)
            for call in tool_calls:
                fn = call["function"]
                args = _parse_tool_arguments(fn.get("arguments"))
                result = await tool_executor(fn["name"], args)
                _record(working_messages, rounds, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })
        # Ran out of rounds without a final answer: one more request with
        # tools off, so the model answers from what it gathered.
        return await _answer_without_tools(client, base_url, api_key, model, working_messages, num_ctx, on_usage)


async def run_turn_stream(base_url: str, model: str, api_key: Optional[str], messages: list[dict],
                           tools: Optional[list[dict]] = None, tool_executor: Optional[Callable[[str, dict], Awaitable[str]]] = None,
                           on_usage: Optional[Callable[[dict], None]] = None, num_ctx: Optional[int] = None,
                           rounds: Optional[list[dict]] = None) -> AsyncIterator[str]:
    """Streaming variant. Tool-calling rounds (if any) are resolved
    non-streamed first — a tool call has no incremental text of its own to
    stream — then only the final round streams token-by-token, same
    real-time feel as before for the common no-tool-call case.

    on_usage, rounds: see run_turn's docstring.
    num_ctx: see module docstring. The plain (no-tools) branch below is SSE-
    based and can't go through _post_chat()/chat_capped() — for a detected
    capped-Ollama endpoint it instead falls back to one non-streamed
    chat_capped() call and yields the whole reply at once (correctness over
    token-by-token smoothness for that specific case)."""
    if not tools:
        if native_api.mode(base_url):
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                data = await native_api.post(client, base_url, model, api_key, messages)
            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            content = data["choices"][0]["message"].get("content") or ""
            if content:
                yield content
            return
        if num_ctx and ollama_client.is_ollama_url(base_url):
            data = await ollama_client.chat_capped(model, messages, num_ctx, base_url=base_url)
            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            content = data["choices"][0]["message"].get("content") or ""
            if content:
                yield content
            return

        stream_body = {"model": model, "messages": _request_messages(base_url, model, messages), "stream": True,
                       "stream_options": {"include_usage": True}}
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", headers=_headers(api_key), json=stream_body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if on_usage and chunk.get("usage"):
                        on_usage(chunk["usage"])
                    choices = chunk.get("choices") or []
                    delta = choices[0]["delta"].get("content") if choices else None
                    if delta:
                        yield delta
        return

    working_messages = list(messages)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for round_num in range(MAX_TOOL_ROUNDS):
            body = {"model": model, "messages": _request_messages(base_url, model, working_messages), "tools": tools}
            if num_ctx:
                body["num_ctx"] = num_ctx
            try:
                data = await _post_chat(client, base_url, api_key, body)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 422):
                    # Fall back to a plain streamed call with no tools.
                    async for chunk in run_turn_stream(base_url, model, api_key, _without_tool_rounds(working_messages),
                                                       tools=None, on_usage=on_usage, num_ctx=num_ctx):
                        yield chunk
                    return
                raise

            if on_usage and data.get("usage"):
                on_usage(data["usage"])
            message = data["choices"][0]["message"]
            tool_calls = message.get("tool_calls")
            if not tool_calls and tool_executor:
                rescued, prose = _rescue_tool_call(message.get("content"), tools)
                if rescued:
                    tool_calls = [rescued]
                    message = {"role": "assistant", "content": prose, "tool_calls": tool_calls}
            if not tool_calls or not tool_executor:
                content = message.get("content") or ""
                if content:
                    yield content
                return

            _record(working_messages, rounds, message)
            for call in tool_calls:
                fn = call["function"]
                args = _parse_tool_arguments(fn.get("arguments"))
                result = await tool_executor(fn["name"], args)
                _record(working_messages, rounds, {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })
        # Ran out of rounds without a final answer (every round called a
        # tool): this used to end the stream with nothing at all.
        yield await _answer_without_tools(client, base_url, api_key, model, working_messages, num_ctx, on_usage)
