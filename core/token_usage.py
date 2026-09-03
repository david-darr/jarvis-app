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
