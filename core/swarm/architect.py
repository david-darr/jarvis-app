"""Draft a team for someone who knows the goal but not the shape.

David's ask 2026-09-19: not everyone opening Swarm knows which specialists
they want. This proposes a roster from a plain description of the work - a
lead and a few specialists, with responsibilities - which the owner then edits
before anything is created. It is opt-in per click and never runs during
ordinary setup, because it spends real tokens.

The constraint block below is the whole reason this is worth having rather
than a novelty. A model asked to design a team with no limits will cheerfully
propose a researcher who reads your files and an editor who renders your
video, and the owner discovers three steps later that none of it can run -
which is exactly how David's first real company wedged. So the drafting
prompt states what these workers actually are: no shell, no files, no
repository, no vault, no web. Anything needing outside material has to be
shaped as something the owner supplies.

Nothing here decides connections. Which model an agent runs on, and whether
that model is admitted at all, stays a server decision made against real
capability - a suggestion from a model is not a grant.
"""
import asyncio
import json
import os
import re
import shutil
import tempfile

import httpx

MAX_SPECIALISTS = 5
MAX_OUTPUT_TOKENS = 1200
REQUEST_TIMEOUT_SECONDS = 120

WORKER_LIMITS = """These teammates are language models talking to each other through a small set
of tools. They can: read their assignment, message each other, record
findings, submit written results, and (the lead only) assign work and review
it. They cannot run commands, read or write files, browse the web, open a
repository, or reach any outside system. They know only what the owner tells
them and what they already know.

So: roles must be about thinking, planning, drafting, analysing, checking and
deciding. Never propose a role whose job is to fetch, run, install, deploy,
browse or read a file. If the work genuinely needs outside material, write
that into the responsibilities as something the owner will paste in."""

INSTRUCTION = """You are designing a small team to accomplish one goal.

{limits}

Reply with JSON only, no prose around it, in exactly this shape:

{{"rationale": "one sentence on why this shape suits the goal",
  "lead": {{"name": "...", "role": "...", "instructions": "..."}},
  "specialists": [{{"name": "...", "role": "...", "instructions": "..."}}]}}

Rules: between one and {maximum} specialists, chosen because the goal needs
them rather than to fill seats. Names are short and human, like a person on a
team. Roles are two or three words. Instructions are one or two sentences
saying what that teammate owns. The lead coordinates and decides; it does not
do the specialist work itself.

The goal:
{goal}"""


class DraftFailed(RuntimeError):
    """The model did not return a usable team. The form is left untouched."""


def _prompt(goal: str) -> str:
    return INSTRUCTION.format(limits=WORKER_LIMITS, maximum=MAX_SPECIALISTS, goal=goal.strip()[:4000])


def _text(value, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftFailed(f"The draft was missing {field}.")
    return value.strip()[:limit]


def parse(raw: str) -> dict:
    """Pull a team out of whatever the model actually said.

    Models wrap JSON in prose or fences often enough that insisting on a bare
    object would fail for no good reason; anything beyond that is a failed
    draft, which changes nothing rather than half-filling a form.
    """
    if not raw or not raw.strip():
        raise DraftFailed("The model returned nothing.")
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise DraftFailed("The model did not return a team in the expected shape.")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        raise DraftFailed("The model's reply was not valid JSON.")
    if not isinstance(data, dict):
        raise DraftFailed("The model's reply was not a team.")

    lead_raw = data.get("lead")
    if not isinstance(lead_raw, dict):
        raise DraftFailed("The draft had no lead.")
    lead = {"name": _text(lead_raw.get("name"), 60, "a lead name"),
            "role": _text(lead_raw.get("role"), 60, "a lead role"),
            "instructions": (lead_raw.get("instructions") or "").strip()[:1000]}

    specialists = []
    seen = {lead["name"].casefold()}
    for member in (data.get("specialists") or [])[:MAX_SPECIALISTS]:
        if not isinstance(member, dict):
            continue
        try:
            name = _text(member.get("name"), 60, "a specialist name")
            role = _text(member.get("role"), 60, "a specialist role")
        except DraftFailed:
            continue
        if name.casefold() in seen:
            continue                      # two teammates with one name cannot be addressed
        seen.add(name.casefold())
        specialists.append({"name": name, "role": role,
                            "instructions": (member.get("instructions") or "").strip()[:1000]})
    if not specialists:
        raise DraftFailed("The draft had no specialists.")
    return {"rationale": (data.get("rationale") or "").strip()[:500],
            "lead": lead, "specialists": specialists}


async def _ask_openai(endpoint: dict, goal: str) -> str:
    headers = {"Content-Type": "application/json"}
    if endpoint.get("api_key"):
        headers["Authorization"] = f"Bearer {endpoint['api_key']}"
    body = {"model": endpoint.get("model"), "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": _prompt(goal)}]}
    if endpoint.get("num_ctx"):
        body["num_ctx"] = endpoint["num_ctx"]
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.post(f"{(endpoint.get('base_url') or '').rstrip('/')}/chat/completions",
                                     headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


async def _ask_claude(endpoint: dict, goal: str) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock

    scratch = tempfile.mkdtemp(prefix="jarvis-swarm-architect-")
    # One exchange, no tools at all: this is a question, not an agent. The
    # scratch cwd exists only because the CLI wants somewhere to be.
    options = ClaudeAgentOptions(cwd=scratch, max_turns=1, setting_sources=[], allowed_tools=[],
                                 system_prompt="Answer with JSON only.", model=endpoint.get("model") or None,
                                 permission_mode="default")
    client = ClaudeSDKClient(options=options)
    parts: list[str] = []
    try:
        await client.connect()
        await client.query(_prompt(goal))
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        shutil.rmtree(scratch, ignore_errors=True)
    return "\n".join(parts)


async def _ask_codex(endpoint: dict, goal: str) -> str:
    """Codex cannot be a Swarm worker - it has no tool surface here - but it
    can answer a question perfectly well, so it can still draft a team."""
    binary = shutil.which("codex")
    if not binary:
        raise DraftFailed("The Codex CLI is not on PATH.")
    scratch = tempfile.mkdtemp(prefix="jarvis-swarm-architect-")
    args = [binary, "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-C", scratch]
    if endpoint.get("model"):
        args += ["-m", endpoint["model"]]
    args.append("-")
    environment = {**os.environ}
    environment.pop("JARVIS_INTERNAL_TOKEN", None)
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=environment)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(_prompt(goal).encode("utf-8")),
                                           timeout=REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        raise DraftFailed("Codex did not answer in time.")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    said = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            said.append(event["item"].get("text") or "")
    return "\n".join(said)


async def draft(endpoint: dict, goal: str) -> dict:
    """One bounded call, then validation. Raises DraftFailed on anything else."""
    if not goal or not goal.strip():
        raise DraftFailed("Describe what you want the team to do first.")
    kind = (endpoint or {}).get("kind") or "api"
    try:
        if kind == "claude_cli":
            raw = await _ask_claude(endpoint, goal)
        elif kind == "codex_cli":
            raw = await _ask_codex(endpoint, goal)
        else:
            raw = await _ask_openai(endpoint, goal)
    except DraftFailed:
        raise
    except Exception as exc:
        raise DraftFailed(f"That connection could not draft a team: {exc}") from exc
    return parse(raw)
