"""Shared memory + cross-session awareness — David's ask 2026-08-31: "a
shared memory along with cross-session awareness while keeping our token
efficiency... out of the box for all imported AI models both local and API."

Pure functions here, reused by both real paths:
- Claude (core/brain.py) already has native file-tool access to the vault
  (its cwd) — it only needs cross-session search added (search_sessions,
  wired in as an in-process SDK tool, see core/hive_mind_server.py).
- Any non-Claude "bring your own model" endpoint (core/external_brain.py)
  has NEITHER today — a plain OpenAI-compatible chat client with zero tool
  access — so it needs both search_vault and search_sessions, wired in via
  a real OpenAI-style function-calling loop (core/providers/openai_compatible.py).

Token efficiency, stated plainly: every function here returns short
snippets (a bounded window around each match), not full file/session
dumps, and is only ever called on the model's own decision to call a tool —
nothing here is stuffed into every prompt by default.
"""
import asyncio
import os
from typing import Optional

from core.constants import BASE_DIR, REPO_CODE_DIRS
from core.session_manager import session_manager
from core.vault import resolve_vault_dir
from services import skills_service, documents_service
from services.notes_service import notes_service
from services.task_service import task_service
from services.calendar_service import calendar_service
from core.contacts_store import list_contacts as _list_contacts

SPECS_DIR = os.path.join(BASE_DIR, "specs")
MAX_LIST_ITEMS = 20  # same token-efficiency posture as everything else here

SNIPPET_RADIUS = 200  # characters of context kept on each side of a match
MAX_SESSIONS_SCANNED = 100  # newest-first cap so a huge session history stays bounded
MAX_FILES_SCANNED = 500


def _snippet(text: str, idx: int, query_len: int) -> str:
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(text), idx + query_len + SNIPPET_RADIUS)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def search_sessions(query: str, exclude_session_id: Optional[str] = None, max_results: int = 5) -> list[dict]:
    """Keyword search across every chat session's message content — the
    "cross-session awareness" half. Newest-updated sessions scanned first,
    capped at MAX_SESSIONS_SCANNED so this stays bounded regardless of how
    much chat history exists. Returns short snippets, not full messages."""
    query_lower = query.lower()
    results = []
    for meta in session_manager.list_sessions()[:MAX_SESSIONS_SCANNED]:
        if meta["id"] == exclude_session_id:
            continue
        session = session_manager.get_session(meta["id"])
        if not session:
            continue
        for msg in reversed(session.get("messages", [])):
            content = msg.get("content", "")
            idx = content.lower().find(query_lower)
            if idx == -1:
                continue
            results.append({
                "session_id": session["id"],
                "session_title": session["title"],
                "role": msg["role"],
                "snippet": _snippet(content, idx, len(query)),
            })
            break  # one hit per session is enough to point back to it
        if len(results) >= max_results:
            break
    return results


def search_vault(query: str, vault_dir: Optional[str] = None, max_results: int = 5) -> list[dict]:
    """Keyword search across vault notes — the "shared memory" half, for
    models that don't otherwise have any vault file access (external
    endpoints; Claude already has this natively via its own file tools, so
    it isn't wired to call this one). Returns short snippets, not full
    file contents — read_vault_file (below) is the deliberate second step
    for when a model actually wants a specific file in full."""
    vault_dir = vault_dir or resolve_vault_dir()
    query_lower = query.lower()
    results = []
    scanned = 0
    for root, _, files in os.walk(vault_dir):
        for filename in files:
            if not filename.endswith(".md"):
                continue
            scanned += 1
            if scanned > MAX_FILES_SCANNED:
                return results
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            idx = text.lower().find(query_lower)
            if idx == -1:
                continue
            results.append({
                "path": os.path.relpath(path, vault_dir),
                "snippet": _snippet(text, idx, len(query)),
            })
            if len(results) >= max_results:
                return results
    return results


def read_vault_file(relative_path: str, vault_dir: Optional[str] = None, max_chars: int = 4000) -> str:
    """Reads one specific vault file in full (bounded at max_chars so a huge
    note can't blow the context budget) — the deliberate second step after
    search_vault points at a file, for models with no native Read tool."""
    vault_dir = vault_dir or resolve_vault_dir()
    full_path = os.path.realpath(os.path.join(vault_dir, relative_path))
    vault_real = os.path.realpath(vault_dir)
    if os.path.commonpath([full_path, vault_real]) != vault_real:
        raise ValueError("path escapes the vault")
    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


def list_skills() -> list[dict]:
    """All available Skills (portable SKILL.md procedures) — the "methods"
    half of the hive mind (David's ask 2026-09-01: "all models... utilize
    and operate under jarvis's methods, skills, and memory, all as a hive
    mind"). Skills live in data/skills/, a sibling of the vault directory,
    not inside it — so even Claude's own native file tools (scoped to its
    vault cwd) can't see them without this being wired in explicitly, same
    real gap search_sessions closed for cross-session history. Returns
    name + one-line description only, not full bodies — read_skill (below)
    is the deliberate second step."""
    return skills_service.list_skills()


def read_skill(slug: str, max_chars: int = 4000) -> str:
    """Reads one skill's full body (bounded at max_chars, same reasoning as
    read_vault_file) — the deliberate second step after list_skills points
    at one."""
    skill = skills_service.get_skill(slug)
    if skill is None:
        raise ValueError(f"no such skill: {slug}")
    text = skill["body"]
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


# -- Notes / Tasks / Calendar (David's ask 2026-09-01, follow-up: asked
# Claude about upcoming events/tasks and it said there weren't any — real
# gap, Claude's cwd is the vault, and Notes/Tasks/Calendar are app data
# under data/*.json, nowhere near it) --------------------------------------
#
# Deliberately NOT raw file access to data/ — that directory also holds
# auth.json (password hashes), sessions.json (session tokens), and
# model_endpoints.json (encrypted API keys). Giving a model raw read access
# to the whole folder would be a real credential-exposure risk, not a
# hypothetical one. These functions go through the exact same service
# layer (services/notes_service.py etc.) the app's own routes use, so a
# model only ever sees the same shaped data the UI would show it — never
# the underlying storage or anything alongside it.

def list_notes() -> list[dict]:
    """Open (non-completed) Notes — todos/reminders/Active-Priorities-style
    items, capped at MAX_LIST_ITEMS."""
    return notes_service.list_notes(include_completed=False)[:MAX_LIST_ITEMS]


def list_tasks() -> list[dict]:
    """Scheduled/automated Tasks (distinct from Notes' todos — see
    services/task_service.py), capped at MAX_LIST_ITEMS."""
    return task_service.list_tasks()[:MAX_LIST_ITEMS]


def list_upcoming_events(days: int = 14) -> list[dict]:
    """Real Calendar events plus due-dated Notes over the next `days` days
    (services/calendar_service.py's own merge — same one Home's "This Week"
    panel uses), capped at MAX_LIST_ITEMS."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start_iso = now.isoformat()
    end_iso = (now + timedelta(days=days)).isoformat()
    return calendar_service.list_range(start_iso, end_iso)[:MAX_LIST_ITEMS]


# -- Notes / Tasks / Calendar writes (David's ask 2026-09-01: "write into
# items such as events, notes, tasks") — same service-layer functions the
# app's own routes call, so a write lands in the running process's actual
# in-memory state instead of getting silently clobbered on the next
# unrelated save (notes_service/task_service/calendar_service each cache
# their JSON file in memory at import time, not re-read per call).

def create_note(text: str, due_date: Optional[str] = None, project: str = "personal") -> dict:
    return notes_service.create_note(text, due_date=due_date, project=project)


def update_note(note_id: str, **fields) -> dict:
    """fields: any of text, due_date, project, completed."""
    return notes_service.update_note(note_id, **fields)


def delete_note(note_id: str) -> None:
    notes_service.delete_note(note_id)


def create_task(name: str, prompt: str, schedule_kind: str, run_at: Optional[str] = None,
                 interval_seconds: Optional[int] = None, deliver_to_channel: Optional[str] = None) -> dict:
    return task_service.create_task(
        name, prompt, schedule_kind, run_at=run_at,
        interval_seconds=interval_seconds, deliver_to_channel=deliver_to_channel,
    )


def update_task(task_id: str, **fields) -> dict:
    """fields: any of name, prompt, enabled, deliver_to_channel."""
    return task_service.update_task(task_id, **fields)


def delete_task(task_id: str) -> None:
    task_service.delete_task(task_id)


def create_event(title: str, start: str, end: str, all_day: bool = False,
                  location: str = "", description: str = "") -> dict:
    return calendar_service.create_event(title, start, end, all_day=all_day, location=location, description=description)


def update_event(event_id: str, **fields) -> dict:
    """fields: any of title, start, end, all_day, location, description, completed."""
    return calendar_service.update_event(event_id, **fields)


def delete_event(event_id: str) -> None:
    calendar_service.delete_event(event_id)


def list_specs() -> list[str]:
    """Filenames of every spec doc in specs/ — architecture/subsystem notes
    (e.g. auth-security.md), safe to expose in full since they're
    documentation, not secrets or user data."""
    if not os.path.isdir(SPECS_DIR):
        return []
    return sorted(f for f in os.listdir(SPECS_DIR) if f.endswith(".md"))


def read_spec(filename: str, max_chars: int = 6000) -> str:
    """Reads one spec doc in full (bounded at max_chars — these can be
    longer than a vault note or skill). Path-guarded the same way
    read_vault_file is, even though specs/ has no untrusted user input
    today — defense in depth costs nothing here."""
    full_path = os.path.realpath(os.path.join(SPECS_DIR, filename))
    specs_real = os.path.realpath(SPECS_DIR)
    if os.path.commonpath([full_path, specs_real]) != specs_real:
        raise ValueError("path escapes the specs directory")
    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


# -- Documents / Contacts / Task run history (David's ask 2026-09-01,
# closing the three real gaps identified when asked "does the ai model know
# where to grab attachments, documents, sessions, skills, vault,
# calendar_events.json, contacts.json...") — same safe pattern as
# everything above: real service-layer functions, never raw file access. ---

def list_documents() -> list[dict]:
    """Library documents (title/tags/timestamps only, not full content —
    read_document below is the deliberate second step), capped at
    MAX_LIST_ITEMS."""
    return documents_service.list_documents()[:MAX_LIST_ITEMS]


def read_document(doc_id: str, max_chars: int = 6000) -> str:
    """Reads one Library document's full content by id (from
    list_documents), bounded at max_chars."""
    doc = documents_service.get_document(doc_id)
    if doc is None:
        raise ValueError(f"no such document: {doc_id}")
    text = doc["content"]
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


def list_contacts() -> list[dict]:
    """Synced contacts (name/email/phone), capped at MAX_LIST_ITEMS."""
    return _list_contacts()[:MAX_LIST_ITEMS]


def list_task_runs(task_id: Optional[str] = None) -> list[dict]:
    """Recent Task execution history — what a scheduled/automated Task
    actually produced when it last ran (list_tasks only has the
    definition, not this), newest first, capped at MAX_LIST_ITEMS."""
    return task_service.list_runs(task_id)[:MAX_LIST_ITEMS]


# -- Repo dev access (David's ask 2026-09-01: "full access to the whole
# jarvis-app repo, both write and read... to work on developmental
# projects") — Claude gets this natively via its own Read/Write/Edit tools
# once core/brain.py adds REPO_CODE_DIRS to add_dirs; a plain OpenAI-
# compatible external model has no native file tools at all, so it needs
# the equivalent as real function-calling tools. Both are scoped to the
# exact same REPO_CODE_DIRS allow-list (core/constants.py) so every model
# sees the same app-source surface — deliberately excludes data/, same
# credential-exposure reasoning as everywhere else in this file.

def _resolve_repo_path(relative_path: str) -> str:
    full_path = os.path.realpath(os.path.join(BASE_DIR, relative_path.lstrip("/\\")))
    if not any(
        full_path == d or os.path.commonpath([full_path, d]) == d
        for d in REPO_CODE_DIRS if os.path.isdir(d)
    ):
        raise ValueError(
            f"'{relative_path}' is outside the app-source directories a model can access "
            f"({', '.join(os.path.basename(d) for d in REPO_CODE_DIRS)})"
        )
    return full_path


def list_repo_directory(relative_path: str = "") -> list[str]:
    """One level of a jarvis-app source directory (not recursive — call
    again with a sub-path to descend, same two-step pattern as
    search_vault/read_vault_file). relative_path="" lists the top-level
    allowed directories themselves."""
    if not relative_path:
        return [os.path.basename(d) for d in REPO_CODE_DIRS if os.path.isdir(d)]
    full_path = _resolve_repo_path(relative_path)
    if not os.path.isdir(full_path):
        raise ValueError(f"not a directory: {relative_path}")
    entries = sorted(os.listdir(full_path))
    return [e + "/" if os.path.isdir(os.path.join(full_path, e)) else e for e in entries]


def read_repo_file(relative_path: str, max_chars: int = 8000) -> str:
    """Reads one file from jarvis-app's own source (bounded at max_chars,
    same reasoning as read_vault_file)."""
    full_path = _resolve_repo_path(relative_path)
    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"


def write_repo_file(relative_path: str, content: str) -> str:
    """Writes (creates or overwrites) one file under jarvis-app's own
    source. Full-file replacement only — no diff/patch tool for external
    models yet, matching this pass's scope. Creates parent directories if
    they don't exist yet, e.g. for a genuinely new module."""
    full_path = _resolve_repo_path(relative_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} chars to {relative_path}"


# -- Shell execution (David's ask 2026-09-02, modeled on Odysseus's own
# agent-tool: src/agent_tools/subprocess_tools.py) — for external models
# only; Claude already has a native Bash tool (see core/brain.py's
# admin-gated allowed_tools/disallowed_tools). Admin-only, enforced by the
# caller (core/external_brain.py only registers this tool at all when the
# session's owner is admin) — same real gate Odysseus uses
# (owner_is_admin_or_single_user()), no command blocklist on top of it,
# matching their actual safety model exactly.

SHELL_TIMEOUT_SECONDS = 3600  # matches Odysseus's own allowance for real, long-running dev tasks
SHELL_MAX_OUTPUT = 200_000


async def run_shell(command: str, cwd: Optional[str] = None, timeout: int = SHELL_TIMEOUT_SECONDS) -> dict:
    """Runs a shell command. cwd defaults to the jarvis-app repo root (not
    the vault) since the actual use case is verifying/running code the
    model just wrote there. Process is killed on timeout; whatever
    stdout/stderr it produced up to that point is still returned, same as
    Odysseus's own behavior."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or BASE_DIR,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout_b.decode(errors="replace")[:SHELL_MAX_OUTPUT],
            "stderr": stderr_b.decode(errors="replace")[:SHELL_MAX_OUTPUT],
            "exit_code": proc.returncode,
        }
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return {"stdout": "", "stderr": f"Command timed out after {timeout}s", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}
