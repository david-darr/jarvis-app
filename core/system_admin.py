"""System diagnostics, backup export/import, and per-domain wipe — Settings >
Admin > System (David's ask 2026-08-31, matching Odysseus's
routes/diagnostics_routes.py + routes/backup_routes.py + routes/admin_wipe/).

Scoped down from Odysseus's version: their backup includes raw settings
because model-endpoint API keys are meaningful to restore elsewhere. Ours
encrypts secrets (core/secret_storage.py) with a key generated per-install,
so exporting the ciphertext would be useless on another machine — secret
fields are deliberately excluded from export rather than exported broken.
"""
import glob
import os
import shutil
import time

from core import discord_bots_store, model_endpoints, settings as settings_store
from core.atomic_io import write_json_atomic
from core.constants import DATA_DIR
from core.session_manager import SESSIONS_DIR, SESSIONS_INDEX_FILE
from core.vault import resolve_vault_dir
from services.notes_service import NOTES_FILE, notes_service
from services.skills_service import SKILLS_DIR, create_skill, get_skill, list_skills
from services.task_service import TASKS_FILE, task_service

BACKUP_VERSION = 1

WIPE_KINDS = {"chats", "notes", "tasks", "skills"}


def diagnostics() -> dict:
    """Bounded, redacted health snapshot — no secrets, matching Odysseus's
    own "diagnostics must avoid growing into secret dumps" rule."""
    vault_dir = resolve_vault_dir()

    data_size = 0
    for root, _, files in os.walk(DATA_DIR):
        for f in files:
            try:
                data_size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass

    return {
        "vault_dir": vault_dir,
        "vault_exists": os.path.isdir(vault_dir),
        "sessions_count": _session_count(),
        "notes_count": len(notes_service.list_notes()),
        "tasks_count": len(task_service.list_tasks()),
        "skills_count": len(list_skills()),
        "model_endpoints_count": len(model_endpoints.list_endpoints()),
        "data_dir_bytes": data_size,
        "auth_enabled": os.getenv("AUTH_ENABLED", "false").lower() in {"1", "true", "yes"},
        "discord_configured": bool(discord_bots_store.list_bots()),
    }


def _session_count() -> int:
    from core.atomic_io import read_json
    return len(read_json(SESSIONS_INDEX_FILE, {}))


def export_backup() -> dict:
    """Non-secret-bearing sections only — see module docstring. Safe for an
    admin to save/share within reason (still contains note/task text)."""
    raw_settings = settings_store.load_settings()
    safe_settings = {k: v for k, v in raw_settings.items() if not k.endswith("_encrypted")}
    full_skills = []
    for s in list_skills():
        full = get_skill(s["slug"])
        if full:
            full_skills.append(full)
    return {
        "version": BACKUP_VERSION,
        "exported_at": time.time(),
        "settings": safe_settings,
        "notes": notes_service.list_notes(),
        "tasks": task_service.list_tasks(),
        "skills": full_skills,
    }


def import_backup(data: dict) -> dict:
    """Best-effort, section-based merge (matches Odysseus's own import
    semantics — invalid/unrecognized sections are skipped, not fatal). Tasks
    are counted but not reconstructed: they carry scheduler semantics
    (run_at/interval) that aren't safe to blind-import without re-validating
    against the live scheduler."""
    imported = {"settings": 0, "notes": 0, "tasks": 0, "skills": 0}

    settings_section = data.get("settings")
    if isinstance(settings_section, dict):
        safe = {k: v for k, v in settings_section.items() if not k.endswith("_encrypted") and k in settings_store.DEFAULTS}
        if safe:
            settings_store.update_settings(**safe)
            imported["settings"] = len(safe)

    if isinstance(data.get("notes"), list):
        for note in data["notes"]:
            if isinstance(note, dict) and note.get("text"):
                notes_service.create_note(note["text"], note.get("due_date"), note.get("project", "personal"))
                imported["notes"] += 1

    if isinstance(data.get("tasks"), list):
        imported["tasks"] = sum(1 for t in data["tasks"] if isinstance(t, dict) and t.get("name"))

    if isinstance(data.get("skills"), list):
        for skill in data["skills"]:
            if isinstance(skill, dict) and skill.get("slug") and skill.get("body") is not None:
                try:
                    create_skill(skill["slug"], skill.get("description", ""), skill["body"])
                    imported["skills"] += 1
                except (ValueError, FileExistsError):
                    pass

    return imported


def wipe(kind: str) -> None:
    """Global, destructive, per-domain — server enforces the kind allowlist;
    client-side double confirmation is UI protection, not the real gate
    (matches Odysseus's own stated security model for admin wipe)."""
    if kind not in WIPE_KINDS:
        raise ValueError(f"unknown wipe kind: {kind}")

    if kind == "chats":
        if os.path.isdir(SESSIONS_DIR):
            shutil.rmtree(SESSIONS_DIR)
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        write_json_atomic(SESSIONS_INDEX_FILE, {})
    elif kind == "notes":
        write_json_atomic(NOTES_FILE, [])
        notes_service._notes = {}
    elif kind == "tasks":
        write_json_atomic(TASKS_FILE, [])
        task_service._tasks = {}
    elif kind == "skills":
        for skill_dir in glob.glob(os.path.join(SKILLS_DIR, "*")):
            if os.path.isdir(skill_dir):
                shutil.rmtree(skill_dir)
