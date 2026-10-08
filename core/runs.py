"""The shared run contract (roadmap phase 1, 2026-10-05; spec: the vault note
"Run Contracts - Phase 1 (Build Spec)"). One way to describe a model run,
whatever runs it - Claude Code, Codex, a local or API model, or Swarm:

- RunContext: what a run is - where it came from, for whom, on which model.
- RunEvent: what happens during it. The kinds carry Swarm's own names
  (core/swarm/models.py EventKind), so a Swarm worker's stream already is
  one of these.
- Usage: what it cost, in one canonical record from normalize_usage(). Every
  provider's numbers pass through it; nothing else guesses at their shape.
- StopResult: whether a stop is confirmed, and how. Swarm's rule now holds
  for every run: nothing claims a confirmed stop, or complete usage, without
  evidence.
- record(): each run's row in the session store's `runs` table (roadmap
  phase 3, 2026-10-05) - what ran, for whom, how it ended, what it used - and
  its timeline in `run_events` (roadmap phase 8, 2026-10-06): each tool
  started and finished, quota readings, checkpoints, and how it ended.

An adapter (each chat brain) has events(prompt) and cancel(). A run that
fails raises; a run that finished ends with one RESULT event. The adapters'
old text streams are projections of their events, so the text a person sees
is the same text the contract carries.

The usage buckets follow Hermes Agent's CanonicalUsage
(agent/usage_pricing.py, MIT License, Copyright (c) 2025 Nous Research).
"""
import asyncio
import contextlib
import contextvars
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import AsyncIterator, Optional, Protocol

logger = logging.getLogger(__name__)

# Tool input and output carried in an event, and provider extras: enough to
# show and diagnose, never a whole file.
CLIP_LIMIT = 2000
META_LIMIT = 2000
# A run's kept timeline (phase 8): at most this many steps, each detail
# clipped to TIMELINE_CLIP characters.
TIMELINE_LIMIT = 200
TIMELINE_CLIP = 300


class EventKind(StrEnum):
    TEXT = "visible_text"
    TOOL_STARTED = "action_started"
    TOOL_FINISHED = "action_finished"
    USAGE = "usage"
    # An account-level allowance reading (Claude's rate-limit message), not
    # this run's own spend.
    QUOTA = "quota"
    # Swarm's durable progress marker; no chat adapter has one.
    CHECKPOINT = "checkpoint"
    RESULT = "result"


@dataclass(frozen=True)
class RunEvent:
    kind: EventKind
    data: dict = field(default_factory=dict)
    # Provider-only detail kept for diagnosis, bounded by META_LIMIT.
    provider_meta: dict = field(default_factory=dict)


def clip(value, limit: int = CLIP_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def clip_ends(value, limit: int) -> str:
    """Both ends of a long value: a shell command's interesting part is often
    its tail (found 2026-10-06: Codex runs JARVIS's tools as a command whose
    first 300 characters are two long folder paths)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + " … " + text[-(limit - head):]


def event(kind: EventKind, provider_meta: Optional[dict] = None, **data) -> RunEvent:
    meta = provider_meta or {}
    if meta and len(json.dumps(meta, ensure_ascii=False, default=str)) > META_LIMIT:
        meta = {"clipped": clip(meta, META_LIMIT)}
    return RunEvent(kind, data, meta)


def text(chunk: str) -> RunEvent:
    return RunEvent(EventKind.TEXT, {"text": chunk})


def tool_started(call_id: str, name: str, arguments) -> RunEvent:
    return RunEvent(EventKind.TOOL_STARTED, {"id": call_id, "name": name, "input": clip(arguments)})


def tool_finished(call_id: str, ok: bool, output) -> RunEvent:
    return RunEvent(EventKind.TOOL_FINISHED, {"id": call_id, "ok": bool(ok), "output": clip(output or "")})


def result(usage_complete: bool, **provider_meta) -> RunEvent:
    """The run finished. usage_complete only when every provider call it
    made reported its usage."""
    return event(EventKind.RESULT, provider_meta or None, usage_complete=bool(usage_complete))


# -- what a run is --------------------------------------------------------------

@dataclass(frozen=True)
class RunContext:
    surface: str  # chat, task, card, goal, swarm or detached
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_run_id: Optional[str] = None
    session_id: Optional[str] = None
    endpoint_id: Optional[str] = None
    model_kind: Optional[str] = None
    model: Optional[str] = None
    is_admin: bool = False
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    # Defined here and enforced nowhere yet: None everywhere is today's
    # behaviour (per-message timeouts only, no token budget for chats/tasks).
    deadline: Optional[float] = None
    budget_tokens: Optional[int] = None
    computer_events: list = field(default_factory=list, compare=False)
    computer_queue: asyncio.Queue = field(default_factory=asyncio.Queue, compare=False)


# The run in progress, for what it starts (core/helpers.py: a helper's run
# names its parent). Set by complete() and by a chat's streamed turn. Unset
# where a brain runs its tools in a task of its own, as Claude Code's
# in-process tools do (found live 2026-10-06): for a chat, its run in
# progress is also kept by chat id (enter/leave, current_for).
CURRENT: contextvars.ContextVar[Optional[RunContext]] = contextvars.ContextVar("current_run", default=None)
_BY_SESSION: dict[str, RunContext] = {}


def enter(context: RunContext) -> None:
    """A chat's run begins: it is the run in progress for that chat."""
    CURRENT.set(context)
    if context.session_id:
        _BY_SESSION[context.session_id] = context


def leave(context: RunContext) -> None:
    if context.session_id and _BY_SESSION.get(context.session_id) is context:
        del _BY_SESSION[context.session_id]


def current_for(session_id: Optional[str]) -> Optional[RunContext]:
    """The run in progress: this task's, else the chat's."""
    return CURRENT.get() or (_BY_SESSION.get(session_id) if session_id else None)


def computer_event(session_id: Optional[str], kind: str, detail: dict, ok=None) -> None:
    context = current_for(session_id)
    if context is None or len(context.computer_events) >= TIMELINE_LIMIT:
        return
    step = {"at": time.time(), "kind": kind, "name": "computer", "ok": ok,
            "detail": json.dumps(detail, ensure_ascii=False), "seconds": None}
    context.computer_events.append(step)
    context.computer_queue.put_nowait(step)


@dataclass(frozen=True)
class StopResult:
    confirmed: bool
    how: str


class RunAdapter(Protocol):
    def events(self, prompt, stream: bool = True) -> AsyncIterator[RunEvent]:
        """stream=False: the caller wants the whole reply, not tokens, and an
        adapter may make plain requests instead of streaming ones."""
        ...

    async def cancel(self) -> StopResult: ...


# -- what it cost ---------------------------------------------------------------

def _int(value) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _add(a: Optional[int], b: Optional[int]) -> Optional[int]:
    return b if a is None else a if b is None else a + b


@dataclass(frozen=True)
class Usage:
    """One provider call's tokens, or a sum of calls. A field is None when the
    provider did not report it, never a guess.

    prompt_tokens is the whole input the request carried, cached part
    included - the context meter's number. total_tokens is the provider's own
    total when it gives one."""
    prompt_tokens: Optional[int] = None
    uncached_input_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    total_tokens: int = 0
    requests: int = 1

    def __add__(self, other: "Usage") -> "Usage":
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(**{name: _add(getattr(self, name), getattr(other, name))
                        for name in ("prompt_tokens", "uncached_input_tokens", "cache_read_tokens",
                                     "cache_write_tokens", "output_tokens", "reasoning_tokens")},
                     total_tokens=self.total_tokens + other.total_tokens, requests=self.requests + other.requests)


def total(usages) -> Optional[Usage]:
    """The sum of a run's usage events, or None when none was reported."""
    found = [u for u in usages if u is not None]
    if not found:
        return None
    combined = found[0]
    for usage in found[1:]:
        combined = combined + usage
    return combined


def _detail(raw: dict, section: str, key: str) -> Optional[int]:
    details = raw.get(section)
    return _int(details.get(key)) if isinstance(details, dict) else None


def normalize_usage(raw) -> Optional[Usage]:
    """Every usage shape JARVIS receives, as one Usage. None for nothing.

    - Anthropic (Claude Code, the Messages API): `input_tokens` is only the
      uncached remainder; cache reads and writes are reported beside it.
    - Codex (as core/codex_brain.py stores it): `input_tokens` is the whole
      input and `cached_input_tokens` a subset of it.
    - DeepSeek: `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.
    - OpenAI chat completions: `prompt_tokens`, with
      `prompt_tokens_details.cached_tokens` a subset of it. The Responses API
      says `input_tokens` with `input_tokens_details.cached_tokens`.
    The whole prompt is `prompt_tokens` wherever a provider gives it."""
    if not isinstance(raw, dict) or not raw:
        return None
    reported = _int(raw.get("prompt_tokens"))
    inp = _int(raw.get("input_tokens"))
    anthropic = "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw
    if reported and reported > 0:
        prompt = reported
    elif inp is None:
        prompt = None
    elif anthropic:
        prompt = inp + (_int(raw.get("cache_read_input_tokens")) or 0) + (_int(raw.get("cache_creation_input_tokens")) or 0)
    else:
        prompt = inp

    read = write = uncached = None
    if anthropic:
        read, write, uncached = (_int(raw.get("cache_read_input_tokens")),
                                 _int(raw.get("cache_creation_input_tokens")), inp)
    elif "cached_input_tokens" in raw:
        read = _int(raw.get("cached_input_tokens"))
        uncached = None if read is None or inp is None else max(inp - read, 0)
    elif "prompt_cache_hit_tokens" in raw or "prompt_cache_miss_tokens" in raw:
        read, uncached = _int(raw.get("prompt_cache_hit_tokens")), _int(raw.get("prompt_cache_miss_tokens"))
    elif isinstance(raw.get("prompt_tokens_details"), dict) and "cached_tokens" in raw["prompt_tokens_details"]:
        read = _detail(raw, "prompt_tokens_details", "cached_tokens")
        uncached = None if read is None or reported is None else max(reported - read, 0)
    elif isinstance(raw.get("input_tokens_details"), dict) and "cached_tokens" in raw["input_tokens_details"]:
        read = _detail(raw, "input_tokens_details", "cached_tokens")
        uncached = None if read is None or inp is None else max(inp - read, 0)

    output = _int(raw.get("output_tokens"))
    if output is None:
        output = _int(raw.get("completion_tokens"))
    reasoning = _detail(raw, "output_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = _detail(raw, "completion_tokens_details", "reasoning_tokens")
    if reasoning is None:
        reasoning = _int(raw.get("reasoning_output_tokens"))

    whole = _int(raw.get("total_tokens"))
    if whole is None:
        whole = sum(int(v) for k, v in raw.items() if k.endswith("tokens") and isinstance(v, (int, float)))
    return Usage(prompt_tokens=prompt, uncached_input_tokens=uncached, cache_read_tokens=read,
                 cache_write_tokens=write, output_tokens=output, reasoning_tokens=reasoning, total_tokens=whole)


def usage_event(raw) -> Optional[RunEvent]:
    usage = normalize_usage(raw)
    return RunEvent(EventKind.USAGE, {"usage": usage}) if usage is not None else None


def claude_quota(message) -> Optional[dict]:
    """Claude's rate-limit message as an account allowance reading. Utilization
    arrives as a fraction and is kept as a percentage; no number is unknown,
    never zero. (Shared with Swarm's Claude worker.)"""
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
    return {"bucket": getattr(info, "rate_limit_type", None) or "account", "used_percent": percent,
            "status": status, "resets_at": getattr(info, "resets_at", None)}


# -- reading a run ---------------------------------------------------------------

async def text_only(stream: AsyncIterator[RunEvent]) -> AsyncIterator[str]:
    """An adapter's old text stream: the text of its events. Closing this
    closes the events at once, so a stopped turn's process or connection is
    cleaned up now rather than whenever the stream is garbage collected."""
    async with contextlib.aclosing(stream) as items:
        async for item in items:
            if item.kind is EventKind.TEXT:
                yield item.data["text"]


# -- running one to the end ----------------------------------------------------

@dataclass(frozen=True)
class RunOutcome:
    text: str
    usage: Optional[Usage]
    usage_complete: bool
    tool_calls: int


@dataclass
class Tally:
    """What a run has produced so far, as its events go by - and its
    timeline: the steps worth seeing later (phase 8), at most
    TIMELINE_LIMIT, the last one saying when more were left out."""
    parts: list = field(default_factory=list)
    usages: list = field(default_factory=list)
    tool_calls: int = 0
    finished: Optional[RunEvent] = None
    timeline: list = field(default_factory=list)
    _tools: dict = field(default_factory=dict)
    dropped: int = 0

    def step(self, kind: str, name: Optional[str] = None, ok: Optional[bool] = None, detail="",
             seconds: Optional[float] = None) -> None:
        if len(self.timeline) >= TIMELINE_LIMIT:
            self.dropped += 1
            return
        self.timeline.append({"at": time.time(), "kind": kind, "name": name, "ok": ok,
                              "detail": clip_ends(detail, TIMELINE_CLIP) if detail else "", "seconds": seconds})

    def add(self, item: RunEvent) -> None:
        if item.kind is EventKind.TEXT:
            self.parts.append(item.data["text"])
        elif item.kind is EventKind.USAGE:
            self.usages.append(item.data["usage"])
        elif item.kind is EventKind.TOOL_STARTED:
            self._tools[item.data.get("id")] = (item.data.get("name"), time.time())
            self.step("tool_started", item.data.get("name"), detail=item.data.get("input") or "")
        elif item.kind is EventKind.TOOL_FINISHED:
            self.tool_calls += 1
            name, began = self._tools.pop(item.data.get("id"), (None, None))
            self.step("tool_finished", name, bool(item.data.get("ok")), item.data.get("output") or "",
                      round(time.time() - began, 2) if began else None)
        elif item.kind is EventKind.QUOTA:
            self.step("quota", item.data.get("bucket"), detail=item.data)
        elif item.kind is EventKind.CHECKPOINT:
            self.step("checkpoint", detail=item.data)
        elif item.kind is EventKind.RESULT:
            self.finished = item

    @property
    def usage(self) -> Optional[Usage]:
        return total(self.usages)

    @property
    def usage_complete(self) -> bool:
        return self.finished is not None and self.finished.data.get("usage_complete") is True


def record(context: RunContext, tally: Tally, outcome: str, stop: Optional[StopResult] = None,
           detail: str = "") -> None:
    """The run's row in the session store (core/session_manager_store.py
    `runs`). outcome: finished, failed or stopped. Never fails the run."""
    usage = tally.usage
    timeline = list(tally.timeline)
    if context.computer_events:
        timeline = [step for step in timeline if step.get("name") not in ("computer", "mcp__hive_mind__computer")]
        timeline = sorted([*timeline, *context.computer_events], key=lambda step: step["at"])[:TIMELINE_LIMIT]
    if tally.dropped:
        timeline.append({"at": time.time(), "kind": "truncated", "name": None, "ok": None,
                         "detail": f"{tally.dropped} more steps were not kept", "seconds": None})
    ended = {"stopped": ("stopped", None if stop is None else stop.confirmed, stop.how if stop else ""),
             "failed": ("failed", False, detail)}.get(outcome)
    if ended:
        timeline.append({"at": time.time(), "kind": ended[0], "name": None, "ok": ended[1],
                         "detail": clip(ended[2] or "", TIMELINE_CLIP), "seconds": None})
    try:
        from core import session_manager_store
        session_manager_store.record_run({
            "id": context.run_id, "parent_id": context.parent_run_id, "surface": context.surface,
            "session_id": context.session_id, "agent_id": context.agent_id, "task_id": context.task_id,
            "endpoint_id": context.endpoint_id, "model": context.model, "started_at": context.started_at,
            "ended_at": time.time(), "outcome": outcome,
            "stop_confirmed": None if stop is None else int(stop.confirmed),
            "tool_calls": tally.tool_calls, "total_tokens": usage.total_tokens if usage else None,
            "cache_read_tokens": usage.cache_read_tokens if usage else None,
            "usage_complete": int(tally.usage_complete), "detail": (detail or "")[:500],
        }, timeline)
    except Exception:
        logger.exception("could not record run %s", context.run_id)


async def complete(adapter: RunAdapter, prompt, context: RunContext, tally: Optional[Tally] = None) -> RunOutcome:
    """Run to the end: the reply as the old run_turn returned it (joined and
    stripped), its usage summed over every provider call, and how many tools
    it used; the run is recorded either way. Recording its usage on Home is
    the caller's, which knows the endpoint. A caller that passes its own
    tally still has what a stopped or failed run used (2026-10-06)."""
    tally = Tally() if tally is None else tally
    token = CURRENT.set(context)
    if context.session_id and context.surface == "chat":
        _BY_SESSION[context.session_id] = context
    try:
        async with contextlib.aclosing(adapter.events(prompt, stream=False)) as items:
            async for item in items:
                tally.add(item)
        if tally.finished is None:
            raise RuntimeError("the run ended without finishing")
    except (asyncio.CancelledError, GeneratorExit):
        record(context, tally, "stopped")
        raise
    except Exception as e:
        record(context, tally, "failed", detail=f"{type(e).__name__}: {e}")
        raise
    finally:
        CURRENT.reset(token)
        leave(context)
    record(context, tally, "finished")
    outcome = RunOutcome("".join(tally.parts).strip(), tally.usage, tally.usage_complete, tally.tool_calls)
    spent = outcome.usage.total_tokens if outcome.usage else None
    logger.info("run %s (%s) finished: %d tool call(s), %s tokens%s", context.run_id, context.surface,
                outcome.tool_calls, spent if spent is not None else "unreported",
                "" if outcome.usage_complete else " (usage incomplete)")
    return outcome
