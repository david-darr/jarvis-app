"""Resolves and seeds the vault directory — the on-disk Obsidian-shaped
memory store the connected Brain session's cwd points at.

Onboarding (Phase 5) will let a user pick/personalize their vault path and
profile; until that exists, this seeds sensible generic defaults so there's
a real, working vault from first boot rather than an empty directory or
literal unfilled {{placeholder}} tokens.
"""
import os

from core.constants import DATA_DIR
from core import settings as settings_store

DEFAULT_VAULT_DIR = os.path.join(DATA_DIR, "vault")

_VAULT_INDEX = """---
status: active
project: meta
type: index
---
# VAULT INDEX

Read this file at the start of every conversation. It has two jobs: the profile of the person you work for, and the map of this vault.

## Vault Structure

```
00 - Inbox        <- Capture everything, sort later
01 - Daily Notes  <- Dated logs of what got done, one file per day
```

One note lives at the vault root alongside this index: [[Active Priorities]], the master task list.

## How My Memory Works (for the AI)

This vault is your memory. It is external and effectively unlimited. Do not try to hold all of it at once — hold only what the current task needs, and trust everything else is one search away.

## Vault Rules for AI

Every note gets YAML frontmatter (`status`, `project`, `type`). Append to an existing note before creating a new one. Update this index's structure/profile sections as you learn things, but never invent facts about the user — ask or leave a section for them to fill in.
"""

_ACTIVE_PRIORITIES = """---
status: active
project: meta
type: plan
---
# Active Priorities

The single master task list. All open work lives here — check it at the start of every conversation.

## Personal

Nothing tracked yet.
"""


# Onboarding questionnaire areas (David's ask 2026-08-31) — a short list of
# life/work domains a new user can opt into tracking, each mapped to a real
# folder created in their vault. "Personal Tasks" and "Journal" aren't in
# this list because Active Priorities + 01 - Daily Notes already cover them
# for everyone, regardless of questionnaire answers.
AREA_FOLDERS = {
    "work": "02 - Work & Projects",
    "health": "03 - Health & Fitness",
    "finances": "04 - Finances",
    "learning": "05 - Learning",
    "home": "06 - Home & Family",
}


def resolve_vault_dir() -> str:
    """Resolution order: settings.json's user-chosen path (Settings tab's
    "select an existing vault on your device" — David's ask, 2026-08-31) >
    JARVIS_VAULT_DIR env var (dev override) > the seeded default under
    data/vault."""
    vault_dir = settings_store.get_setting("vault_dir") or os.getenv("JARVIS_VAULT_DIR", DEFAULT_VAULT_DIR)
    seed_vault_if_empty(vault_dir)
    return vault_dir


def seed_vault_if_empty(vault_dir: str) -> None:
    """Only ever seeds into a directory that doesn't exist yet or is
    genuinely empty. A user-selected pre-existing vault (even one that
    doesn't happen to have a file named exactly "Vault Index.md") must never
    get our generic starter content mixed into it."""
    if os.path.isdir(vault_dir) and os.listdir(vault_dir):
        return  # non-empty — treat as a real existing vault, never touch it

    os.makedirs(os.path.join(vault_dir, "00 - Inbox"), exist_ok=True)
    os.makedirs(os.path.join(vault_dir, "01 - Daily Notes"), exist_ok=True)

    with open(os.path.join(vault_dir, "Vault Index.md"), "w", encoding="utf-8") as f:
        f.write(_VAULT_INDEX)
    with open(os.path.join(vault_dir, "Active Priorities.md"), "w", encoding="utf-8") as f:
        f.write(_ACTIVE_PRIORITIES)


def _is_untouched_generic_seed(vault_dir: str) -> bool:
    """True only if vault_dir contains exactly the generic seed_vault_if_empty
    output and nothing else — i.e. no real conversation has written to it
    yet. resolve_vault_dir() auto-seeds on first read (e.g. onboarding's own
    GET /api/settings call before the user reaches the vault step), so by the
    time the questionnaire runs the generic files already exist; this lets
    build_custom_vault safely replace them without risking a real user's
    vault that merely happens to look empty otherwise."""
    if not os.path.isdir(vault_dir):
        return False
    entries = set(os.listdir(vault_dir))
    if entries != {"00 - Inbox", "01 - Daily Notes", "Vault Index.md", "Active Priorities.md"}:
        return False
    if os.listdir(os.path.join(vault_dir, "00 - Inbox")) or os.listdir(os.path.join(vault_dir, "01 - Daily Notes")):
        return False
    with open(os.path.join(vault_dir, "Vault Index.md"), encoding="utf-8") as f:
        if f.read() != _VAULT_INDEX:
            return False
    with open(os.path.join(vault_dir, "Active Priorities.md"), encoding="utf-8") as f:
        if f.read() != _ACTIVE_PRIORITIES:
            return False
    return True


def build_custom_vault(vault_dir: str, areas: list[str], profile_note: str = "") -> bool:
    """Onboarding questionnaire (David's ask 2026-08-31): replaces the
    generic seed with a structure shaped to the areas of life/work the user
    said they want tracked, plus whatever they told us about themselves
    written straight into the index instead of left as a blank placeholder.

    Only acts on a brand-new directory or one that's still exactly the
    untouched generic seed (see _is_untouched_generic_seed) — a real vault
    with real content, whether user-selected or already used, is never
    touched. Returns whether it actually wrote anything.
    """
    is_new = not os.path.isdir(vault_dir) or not os.listdir(vault_dir)
    if not is_new and not _is_untouched_generic_seed(vault_dir):
        return False

    os.makedirs(os.path.join(vault_dir, "00 - Inbox"), exist_ok=True)
    os.makedirs(os.path.join(vault_dir, "01 - Daily Notes"), exist_ok=True)
    folder_lines = [
        "00 - Inbox        <- Capture everything, sort later",
        "01 - Daily Notes  <- Dated logs of what got done, one file per day",
    ]
    for key in areas:
        folder = AREA_FOLDERS.get(key)
        if not folder:
            continue
        os.makedirs(os.path.join(vault_dir, folder), exist_ok=True)
        folder_lines.append(folder)

    profile_section = profile_note.strip() or "Ask or leave this for the user to fill in."
    index = f"""---
status: active
project: meta
type: index
---
# VAULT INDEX

Read this file at the start of every conversation. It has two jobs: the profile of the person you work for, and the map of this vault.

## Profile

{profile_section}

## Vault Structure

```
{chr(10).join(folder_lines)}
```

One note lives at the vault root alongside this index: [[Active Priorities]], the master task list.

## How My Memory Works (for the AI)

This vault is your memory. It is external and effectively unlimited. Do not try to hold all of it at once — hold only what the current task needs, and trust everything else is one search away.

## Vault Rules for AI

Every note gets YAML frontmatter (`status`, `project`, `type`). Append to an existing note before creating a new one. Update this index's structure/profile sections as you learn things, but never invent facts about the user — ask or leave a section for them to fill in.
"""
    with open(os.path.join(vault_dir, "Vault Index.md"), "w", encoding="utf-8") as f:
        f.write(index)
    with open(os.path.join(vault_dir, "Active Priorities.md"), "w", encoding="utf-8") as f:
        f.write(_ACTIVE_PRIORITIES)
    return True
