"""Per-model token usage tracking — the Home tab's "AI Models" card
(David's ask 2026-09-01: "a card listing the ai models added along with
their token usage percentage"). Best-effort by nature: Brain/ExternalBrain
only report usage when the underlying SDK/endpoint actually includes it on a
given turn (see core/brain.py's ResultMessage.usage and
core/providers/openai_compatible.py's on_usage docstring) — a turn with no
usage data simply doesn't add to the total, not an error.

JSON-backed running totals, one integer per endpoint id, same atomic-write
convention as every other simple counter store in this project.
"""
import os
import time

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.runs import Usage, normalize_usage

USAGE_FILE = os.path.join(DATA_DIR, "token_usage.json")


def _extract_total_tokens(usage: dict) -> int:
    """A turn's total: the provider's own `total_tokens` when it gives one
    (so prompt and completion are not counted twice against it), else every
    *_tokens figure summed (the Claude Agent SDK reports no total). See
    core/runs.py normalize_usage, the one place usage shapes are read."""
    normalized = normalize_usage(usage)
    return normalized.total_tokens if normalized else 0


def extract_context_tokens(usage: dict | None) -> int | None:
    """How many tokens the LAST turn's prompt actually occupied — the
    numerator for the chat's context indicator (David's ask 2026-09-15).

    This is deliberately NOT derived from the running totals above. Those
    accumulate spend across every turn forever; context occupancy is a
    point-in-time measurement that goes DOWN after a compaction and resets
    when a thread restarts. Summing historical input tokens into a
    "percentage of context used" would produce a number that only ever
    climbs and crosses 100% on any long chat — confidently wrong, which is
    worse than showing nothing.

    Returns None when the provider reported nothing usable, so the caller
    can say "unavailable" rather than render a fabricated figure. Each
    provider's arithmetic (Claude's input excluding its cache, Codex's
    including it) is in core/runs.py normalize_usage.
    """
    normalized = normalize_usage(usage)
    return (normalized.prompt_tokens or None) if normalized else None


def extract_cache_tokens(usage: dict | None) -> dict:
    """How the last turn's prompt split between the prompt cache and fresh
    input (the prompt-cache audit, 2026-09-22: JARVIS kept no record of
    cache reads at all, so a miss was invisible). Each value is None when the
    provider did not report it, never a guess; see core/runs.py
    normalize_usage for each provider's shape."""
    normalized = normalize_usage(usage)
    if normalized is None:
        return {"cache_read_tokens": None, "cache_write_tokens": None, "uncached_input_tokens": None}
    return {"cache_read_tokens": normalized.cache_read_tokens, "cache_write_tokens": normalized.cache_write_tokens,
            "uncached_input_tokens": normalized.uncached_input_tokens}


def build_context_state(usage: dict | None, capacity: dict | None, model_id: str | None) -> dict | None:
    """Assemble what the chat header renders, or None when there's nothing
    honest to show. `percent` is present only when BOTH a real measurement
    and a known capacity exist; a known token count with an unknown capacity
    still returns the count, with percent None, so the UI can show "12,400
    tokens" instead of either a guess or a blank."""
    used = extract_context_tokens(usage)
    if used is None:
        return None
    state = {
        "used_tokens": used,
        "model": model_id,
        "capacity_tokens": None,
        "percent": None,
        # True when the capacity figure is our own curated estimate rather
        # than something the provider published — surfaced in the UI so a
        # rough number is never mistaken for a measured one.
        "estimated_capacity": False,
        "capacity_source": None,
        "updated_at": time.time(),
        # Last turn's prompt-cache split; see extract_cache_tokens().
        **extract_cache_tokens(usage),
    }
    if capacity and capacity.get("effective"):
        effective = int(capacity["effective"])
        state["capacity_tokens"] = effective
        state["percent"] = round(min(used / effective, 1.0) * 100, 1)
        state["estimated_capacity"] = bool(capacity.get("estimated"))
        state["capacity_source"] = capacity.get("source")
    return state


def _entry(value) -> dict:
    """One endpoint's stored totals. A bare integer is the format before
    2026-09-22, when fresh input and cache reads were summed together; it is
    kept as "unsplit" rather than guessed apart."""
    if isinstance(value, dict):
        return {k: int(value.get(k) or 0) for k in ("fresh_tokens", "cache_read_tokens", "unsplit_tokens")}
    return {"fresh_tokens": 0, "cache_read_tokens": 0, "unsplit_tokens": int(value or 0)}


def record_usage(endpoint_id: str, usage: "Usage | dict | None") -> None:
    """Add one turn to an endpoint's lifetime totals, keeping cache reads
    apart from fresh tokens (new input, cache writes, output). A cache read
    is the provider reusing a prompt it already has, at a fraction of the
    price, so summing it in made a long, cache-warm Claude chat look like
    ten times the spend it was.

    `usage` is a whole turn's Usage (every provider call summed, core/runs.py)
    or one provider's raw usage dict."""
    normalized = usage if isinstance(usage, Usage) else normalize_usage(usage)
    if normalized is None or normalized.total_tokens <= 0:
        return
    total = normalized.total_tokens
    cache_read = normalized.cache_read_tokens or 0
    data = read_json(USAGE_FILE, {})
    entry = _entry(data.get(endpoint_id))
    entry["cache_read_tokens"] += cache_read
    entry["fresh_tokens"] += max(total - cache_read, 0)
    data[endpoint_id] = entry
    write_json_atomic(USAGE_FILE, data)


def get_usage_summary() -> dict[str, dict]:
    """{endpoint_id: {"fresh_tokens", "cache_read_tokens", "unsplit_tokens",
    "total_tokens"}} - tokens this JARVIS install has sent and received
    through each endpoint. There is deliberately no percentage: the old one
    was each endpoint's share of the combined total, which the Home card
    labelled "% used" as if it were a quota. A subscription's real quota
    comes from the provider, never from these counts. An endpoint that has
    never reported usage is absent, not shown at zero."""
    summary = {}
    for endpoint_id, value in read_json(USAGE_FILE, {}).items():
        entry = _entry(value)
        entry["total_tokens"] = sum(entry.values())
        if entry["total_tokens"] > 0:
            summary[endpoint_id] = entry
    return summary
