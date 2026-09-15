"""Per-provider model catalog — the searchable model list and reasoning-effort
levels behind the chat composer's model picker (David's ask 2026-09-15), and
the capacity denominator behind the context indicator (see
core/token_usage.py's context helpers and services/chat_service.py).

Why this isn't one hardcoded table
----------------------------------
The two CLI providers differ in what they'll actually tell us, so this module
treats them differently rather than inventing a uniform fiction:

- **codex_cli** reads Codex's OWN local catalog at `$CODEX_HOME/models_cache.json`
  (default `~/.codex`). Verified live before writing this: the file is real,
  refreshed by the CLI itself, and carries exactly the fields needed —
  `slug`, `display_name`, `description`, `default_reasoning_level`,
  `supported_reasoning_levels` (each with its own description),
  `context_window`, `max_context_window`, `effective_context_window_percent`,
  and a `visibility` flag ("list" vs "hide"). That means the model names,
  the effort levels, AND the context capacity for Codex are all real
  provider data read from disk — not guesses, and no network call. This is
  deliberately the CLI's own catalog rather than the public API's model list:
  the handoff's warning that "API model availability does not equal access
  through the connected CLI" is exactly right, and this file IS what the
  connected CLI believes it can reach.

- **claude_cli** has no equivalent. Checked before falling back to a curated
  table: `claude --help` documents aliases and full names but offers no
  non-interactive model list, and `~/.claude/stats-cache.json` — the only
  local file carrying a `contextWindow` field — was stale (last computed
  months earlier), listed a single superseded model, and reported
  `contextWindow: 0`. Unusable, so it is not read. Claude entries below are
  curated and honestly flagged `source: "curated"` / `estimated: True`, and
  the UI says so rather than presenting them as measured.

Codex's own protocol schema (`codex app-server generate-json-schema`) defines
`ReasoningEffort` as "a non-empty reasoning effort value advertised by the
model" — an open string, explicitly NOT an enum, with per-model
`supportedReasoningEfforts`. So no effort list is hardcoded for Codex; the
normalized shape below mirrors that schema (`id`/`display_name`/
`supported_efforts`/`default_effort`) specifically so swapping in a real
App Server `model/list` later is a drop-in replacement for the cache read,
not a rewrite of every caller.

Effort is always OPTIONAL. None means "don't pass one at all," which is the
exact behavior every session had before this module existed — an unset effort
never changes a turn.
"""
import logging
import os
import typing

from core.atomic_io import read_json

logger = logging.getLogger(__name__)

# Guard against reading something pathological out of the user's home
# directory — the real file is ~220KB; anything wildly beyond that is not the
# catalog we're expecting and isn't worth parsing.
_MAX_CACHE_BYTES = 8 * 1024 * 1024

# Only these two endpoint kinds have a model/effort catalog. local/api
# endpoints already carry their own explicit model string in the endpoint
# record (core/model_endpoints.py) and are configured in Settings, not here.
CATALOG_KINDS = ("claude_cli", "codex_cli")

# Fallback only — the live value is derived from the installed SDK in
# _claude_efforts() below, so a newer SDK that adds a level is picked up
# without editing this list.
_CLAUDE_EFFORT_FALLBACK = ("low", "medium", "high", "xhigh", "max")

_CLAUDE_EFFORT_DESCRIPTIONS = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra reasoning depth for hard problems",
    "max": "Maximum reasoning depth",
}

# Curated, and labelled as such everywhere it surfaces. Context windows are
# the documented standard sizes; the SDK's 1M-token context is a separate
# opt-in beta (ClaudeAgentOptions.betas) this app does not enable, so the
# standard figure is the honest one to show.
_CLAUDE_MODELS = (
    {"id": "claude-opus-5", "display_name": "Opus 5", "alias": "opus",
     "description": "Most capable Claude model for complex work.", "context_window": 200_000},
    {"id": "claude-sonnet-5", "display_name": "Sonnet 5", "alias": "sonnet",
     "description": "Balanced capability and speed.", "context_window": 200_000},
    {"id": "claude-fable-5", "display_name": "Fable 5", "alias": "fable",
     "description": "Latest Fable generation.", "context_window": 200_000},
    {"id": "claude-haiku-4-5-20251001", "display_name": "Haiku 4.5", "alias": None,
     "description": "Fastest, lightest Claude model.", "context_window": 200_000},
)


def _claude_efforts() -> tuple[str, ...]:
    """Read the effort levels the INSTALLED SDK actually accepts, rather than
    trusting a list written here. ClaudeAgentOptions.effort is a
    `Optional[Literal[...]]`, so the literal's own args are the authoritative
    set for this machine's SDK version. Falls back to the constant above if
    the SDK's shape ever changes."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions
        hints = typing.get_type_hints(ClaudeAgentOptions)
        for arg in typing.get_args(hints["effort"]):
            values = typing.get_args(arg)
            if values and all(isinstance(v, str) for v in values):
                return tuple(values)
    except Exception as e:  # SDK missing, renamed field, changed annotation
        logger.debug("model_catalog: falling back to static Claude effort list (%s)", e)
    return _CLAUDE_EFFORT_FALLBACK


def _codex_cache_path() -> str:
    """`$CODEX_HOME` is Codex's own documented override (its `-c` help text
    refers to `$CODEX_HOME/config.toml`), so honour it rather than assuming
    the default location."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "models_cache.json")


# (mtime, size) -> parsed models. The cache file is ~220KB and the picker can
# be re-rendered on every session switch, so it is parsed only when the file
# on disk actually changes.
_codex_cache: tuple[tuple[float, int], list[dict]] | None = None


def _load_codex_models() -> list[dict]:
    global _codex_cache
    path = _codex_cache_path()
    try:
        stat = os.stat(path)
    except OSError:
        return []
    if stat.st_size > _MAX_CACHE_BYTES:
        logger.warning("model_catalog: %s is unexpectedly large (%d bytes) — not parsed.", path, stat.st_size)
        return []
    stamp = (stat.st_mtime, stat.st_size)
    if _codex_cache is not None and _codex_cache[0] == stamp:
        return _codex_cache[1]

    raw = read_json(path, None)
    models = (raw or {}).get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        _codex_cache = (stamp, [])
        return []

    out = []
    for m in models:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug")
        # "hide" marks internal/system entries (e.g. the auto-review model) —
        # they are not user-selectable chat models, so they stay out of the
        # picker exactly as the CLI's own UI leaves them out.
        if not slug or m.get("visibility") == "hide":
            continue
        efforts = []
        for level in m.get("supported_reasoning_levels") or []:
            if isinstance(level, dict) and level.get("effort"):
                efforts.append({"effort": level["effort"], "description": level.get("description") or ""})
        out.append({
            "id": slug,
            "display_name": m.get("display_name") or slug,
            # model_messages/availability_nux carry long prompt text — never
            # forwarded to the client; only these display fields are.
            "description": m.get("description") or "",
            "default_effort": m.get("default_reasoning_level"),
            "supported_efforts": efforts,
            "context_window": m.get("context_window"),
            "effective_context_percent": m.get("effective_context_window_percent"),
            "source": "cli_cache",
            "estimated": False,
        })
    out.sort(key=lambda e: (e["display_name"] or "").lower())
    _codex_cache = (stamp, out)
    return out


def _claude_catalog() -> list[dict]:
    efforts = [{"effort": e, "description": _CLAUDE_EFFORT_DESCRIPTIONS.get(e, "")} for e in _claude_efforts()]
    return [{
        "id": m["id"],
        "display_name": m["display_name"],
        "description": m["description"],
        "alias": m["alias"],
        # No per-model effort data exists for Claude the way Codex publishes
        # it, and the docs are explicit that supported levels vary by model
        # and account. The SDK-derived set is offered uniformly and the UI
        # says levels may vary — passing one is opt-in, never automatic.
        "default_effort": None,
        "supported_efforts": efforts,
        "context_window": m["context_window"],
        "effective_context_percent": None,
        "source": "curated",
        "estimated": True,
    } for m in _CLAUDE_MODELS]


def list_models(kind: str) -> list[dict]:
    """Selectable models for an endpoint kind. Empty list (not an error) for
    a kind with no catalog, or when Codex's cache isn't present — the custom
    model-ID field stays available either way, so the picker degrades to
    exactly the pre-catalog behaviour instead of blocking."""
    if kind == "codex_cli":
        return _load_codex_models()
    if kind == "claude_cli":
        return _claude_catalog()
    return []


def get_model(kind: str, model_id: str | None) -> dict | None:
    if not model_id:
        return None
    for entry in list_models(kind):
        if entry["id"] == model_id or entry.get("alias") == model_id:
            return entry
    return None


def supported_efforts(kind: str, model_id: str | None) -> list[str]:
    """Effort values valid for a specific model. A model that isn't in the
    catalog (a custom ID the user typed) has no advertised list, so the
    provider-wide union is used — see validate_effort() for why that's the
    honest ceiling rather than either rejecting outright or accepting
    anything."""
    entry = get_model(kind, model_id)
    if entry:
        return [e["effort"] for e in entry["supported_efforts"]]
    union: list[str] = []
    for m in list_models(kind):
        for e in m["supported_efforts"]:
            if e["effort"] not in union:
                union.append(e["effort"])
    return union


def validate_effort(kind: str, model_id: str | None, effort: str | None) -> bool:
    """None is always valid — it means "send no effort at all", the
    pre-existing behaviour. Otherwise the value must be one this provider
    actually advertises.

    This is the real gate: `codex exec --strict-config` validates unknown
    config KEYS but not their values, so an invalid effort would sail past
    the CLI's own parsing and only surface as a failed turn. Verified live
    before relying on it.
    """
    if effort is None:
        return True
    if kind not in CATALOG_KINDS:
        return False
    return effort in supported_efforts(kind, model_id)


def context_capacity(kind: str, model_id: str | None) -> dict | None:
    """Capacity denominator for the context indicator, or None when nothing
    trustworthy is known (the indicator then reports "unavailable" rather
    than inventing a percentage).

    `effective` is what the meter divides by: Codex publishes an
    `effective_context_window_percent` (95) below the raw window, which is
    the usable share before its own truncation kicks in, so showing the raw
    number would understate how full the context really is.
    """
    entry = get_model(kind, model_id)
    if not entry or not entry.get("context_window"):
        return None
    window = int(entry["context_window"])
    percent = entry.get("effective_context_percent")
    effective = int(window * percent / 100) if isinstance(percent, (int, float)) and percent else window
    return {
        "window": window,
        "effective": effective,
        "estimated": bool(entry.get("estimated")),
        "source": entry.get("source"),
    }
