"""Native Anthropic Messages and OpenAI Responses transports for API models.

Both return the small Chat Completions shape used by JARVIS's existing tool
loop. This keeps model discovery separate from the agent's memory tools.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx


def mode(base_url: str) -> str | None:
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return None
    host = (parsed.hostname or "").lower()
    if host == "api.anthropic.com":
        return "anthropic"
    if host == "api.openai.com":
        return "openai"
    return None


def _anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system: list[str] = []
    converted: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            system.append(str(content))
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": str(content)}
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
        elif role in ("user", "assistant"):
            if role == "assistant" and message.get("_anthropic_content"):
                converted.append({"role": role, "content": message["_anthropic_content"]})
                continue
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": call["id"], "name": fn["name"], "input": args})
            if blocks:
                converted.append({"role": role, "content": blocks})
    return "\n\n".join(system), converted


async def _anthropic_post(client: httpx.AsyncClient, model: str, key: str,
                          messages: list[dict], tools: list[dict] | None) -> dict:
    system, converted = _anthropic_messages(messages)
    body: dict = {"model": model, "max_tokens": 4096, "messages": converted}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["function"]["name"],
                          "description": t["function"].get("description", ""),
                          "input_schema": t["function"]["parameters"]}
                         for t in tools if t.get("type") == "function"]
    response = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json=body)
    response.raise_for_status()
    data = response.json()
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    calls = [{"id": block["id"], "type": "function",
              "function": {"name": block["name"], "arguments": json.dumps(block.get("input") or {})}}
             for block in data.get("content", []) if block.get("type") == "tool_use"]
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    return {"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": calls,
                                     "_anthropic_content": data.get("content") or []}}],
            "usage": {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": cache_write, "output_tokens": output_tokens,
                      "total_tokens": input_tokens + cache_read + cache_write + output_tokens}}


def _responses_input(messages: list[dict]) -> list[dict]:
    items = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": str(message.get("content") or "")})
        elif role in ("system", "user", "assistant"):
            if message.get("_response_items"):
                # Stateless Responses reasoning models need their prior
                # reasoning and function-call items back with tool outputs.
                items.extend(message["_response_items"])
                continue
            if message.get("content"):
                items.append({"role": role, "content": message["content"]})
            for call in message.get("tool_calls") or []:
                fn = call["function"]
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": fn["name"], "arguments": fn.get("arguments") or "{}"})
    return items


async def _openai_post(client: httpx.AsyncClient, model: str, key: str,
                       messages: list[dict], tools: list[dict] | None) -> dict:
    body: dict = {"model": model, "input": _responses_input(messages), "store": False,
                  "include": ["reasoning.encrypted_content"]}
    if tools:
        body["tools"] = [{"type": "function", "name": t["function"]["name"],
                          "description": t["function"].get("description", ""),
                          "parameters": t["function"]["parameters"]}
                         for t in tools if t.get("type") == "function"]
    response = await client.post("https://api.openai.com/v1/responses",
                                 headers={"Authorization": f"Bearer {key}"}, json=body)
    response.raise_for_status()
    data = response.json()
    text_parts: list[str] = []
    calls: list[dict] = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            text_parts.extend(part.get("text", "") for part in item.get("content") or []
                              if part.get("type") == "output_text")
        elif item.get("type") == "function_call":
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item.get("arguments") or "{}"}})
    return {"choices": [{"message": {"role": "assistant", "content": "".join(text_parts),
                                     "tool_calls": calls, "_response_items": data.get("output") or []}}],
            "usage": data.get("usage") or {}}


async def post(client: httpx.AsyncClient, base_url: str, model: str, key: str | None,
               messages: list[dict], tools: list[dict] | None = None) -> dict:
    if not key:
        raise ValueError("This API connection needs a saved API key")
    provider = mode(base_url)
    if provider == "anthropic":
        return await _anthropic_post(client, model, key, messages, tools)
    if provider == "openai":
        return await _openai_post(client, model, key, messages, tools)
    raise ValueError("Not a native API connection")
