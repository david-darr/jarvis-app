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

USAGE_FILE = os.path.join(DATA_DIR, "token_usage.json")


def _extract_total_tokens(usage: dict) -> int:
    """Handles both usage shapes this app ever sees: OpenAI-compatible
    (`{"prompt_tokens", "completion_tokens", "total_tokens"}` — prefer the
    explicit total so prompt+completion aren't double-counted against it)
    and the Claude Agent SDK's (`{"input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens"}` — no single
    total field, so sum every *_tokens integer present)."""
    if not usage:
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)):
        return int(total)
    return sum(int(v) for k, v in usage.items() if k.endswith("tokens") and isinstance(v, (int, float)))


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
    can say "unavailable" rather than render a fabricated figure.

    Distinguished by key shape, since each provider names these differently
    and the distinction genuinely changes the arithmetic:

    - OpenAI-compatible (`prompt_tokens`): already the whole prompt.
    - Claude Agent SDK (`cache_read_input_tokens` /
      `cache_creation_input_tokens`): `input_tokens` counts ONLY the
      uncached remainder, so the cached portions must be added back or a
      cache-warm turn reads as a near-empty context.
    - Codex (`cached_input_tokens`): `input_tokens` is already the full
      input and the cached figure is a subset of it (see
      core/codex_brain.py's turn.completed handler) — adding them would
      double-count.
    """
    if not usage:
        return None
    prompt = usage.get("prompt_tokens")
    if isinstance(prompt, (int, float)) and prompt > 0:
        return int(prompt)
    inp = usage.get("input_tokens")
    if not isinstance(inp, (int, float)):
        return None
    total = int(inp)
    if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
    return total or None


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
    }
    if capacity and capacity.get("effective"):
        effective = int(capacity["effective"])
        state["capacity_tokens"] = effective
        state["percent"] = round(min(used / effective, 1.0) * 100, 1)
        state["estimated_capacity"] = bool(capacity.get("estimated"))
        state["capacity_source"] = capacity.get("source")
    return state


def record_usage(endpoint_id: str, usage: dict | None) -> None:
    tokens = _extract_total_tokens(usage or {})
    if tokens <= 0:
        return
    data = read_json(USAGE_FILE, {})
    data[endpoint_id] = data.get(endpoint_id, 0) + tokens
    write_json_atomic(USAGE_FILE, data)


def get_usage_summary() -> dict[str, dict]:
    """{endpoint_id: {"total_tokens": int, "percentage": float}} — percentage
    is this endpoint's share of the combined total across every endpoint
    that has ever reported usage, not a percentage of any fixed budget/cap
    (this app doesn't have one). An endpoint that's never reported usage
    (or was just added) is simply absent, not shown at 0%."""
    data = read_json(USAGE_FILE, {})
    grand_total = sum(data.values())
    if grand_total <= 0:
        return {}
    return {
        endpoint_id: {"total_tokens": tokens, "percentage": round(tokens / grand_total * 100, 1)}
        for endpoint_id, tokens in data.items()
    }
