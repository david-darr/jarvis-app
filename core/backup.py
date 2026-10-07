"""Back up everything, and restore it (roadmap phase 8, 2026-10-06; spec:
the vault note "Operations - Phase 8 (Build Spec)").

Settings' older backup (core/system_admin.py) exports settings, notes, tasks
and skills as JSON. This one is the whole data folder - chats, agents and
their memory, triggers, hooks, connectors, integrations, run history, model
connections - as one zip:

- SQLite stores (the session store, Swarm's) are copied with SQLite's backup
  API, so the copy is consistent even while Kairos writes.
- Saved passwords and keys are left out unless asked for. The encryption key
  (.secret_key) lives in the data folder, so a backup carrying it is as
  sensitive as the passwords themselves. Without it, every encrypted value
  is blanked: a restored connection asks for its key again rather than
  failing to decrypt.
- Logs, file checkpoints and review screenshots stay out (history that is
  large and rebuilt), and so does the vault: it is the person's own folder,
  synced their own way.

Restoring swaps the backup in at the next start (apply_pending_restore, run
by app.py before anything reads the data folder), because the live files are
open. The current data is kept first as a safety copy under
.restore/before-restore-<time>; a restore that fails part-way puts it back.
"""
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from typing import Optional

logger = logging.getLogger(__name__)

MANIFEST = "jarvis-backup.json"
VERSION = 1
KEY_FILE = ".secret_key"
RESTORE_DIR = ".restore"
PENDING = "pending.zip"
LAST = "last.json"
# Never backed up: rebuilt or large history, and the restore area itself.
SKIPPED_DIRS = {"logs", "file_checkpoints", RESTORE_DIR, "worktrees", "ui-review", "chat-review",
                "browser-review", "usage-overlay-review", "swarm-codex-review", "__pycache__"}
SKIPPED_SUFFIXES = ("-wal", "-shm", ".tmp")
MAX_RESTORE_BYTES = 4 * 1024 ** 3


def _data_dir() -> str:
    from core.constants import DATA_DIR
    return DATA_DIR


def _vault_inside(data_dir: str) -> Optional[str]:
    try:
        from core.vault import resolve_vault_dir
        vault = os.path.realpath(resolve_vault_dir())
    except Exception:
        return None
    root = os.path.realpath(data_dir)
    return vault if os.path.commonpath([vault, root]) == root else None


def _files(data_dir: str):
    vault = _vault_inside(data_dir)
    for folder, dirs, files in os.walk(data_dir):
        dirs[:] = [d for d in dirs if d not in SKIPPED_DIRS and not d.startswith("pre-")
                   and os.path.realpath(os.path.join(folder, d)) != vault
                   and not os.path.islink(os.path.join(folder, d))]
        for name in files:
            path = os.path.join(folder, name)
            if name.endswith(SKIPPED_SUFFIXES) or ".corrupt-" in name or ".pre-v" in name or os.path.islink(path):
                continue
            yield path, os.path.relpath(path, data_dir).replace(os.sep, "/")


def _blank_secrets(value):
    """Every encrypted value blanked: keys ending _encrypted, and any Fernet
    token (core/secret_storage.py's output always starts gAAAAA)."""
    if isinstance(value, dict):
        return {k: (None if k.endswith("_encrypted") else _blank_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank_secrets(v) for v in value]
    if isinstance(value, str) and value.startswith("gAAAAA") and len(value) > 80:
        return None
    return value


def _sqlite_copy(path: str, target: str) -> None:
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def make_backup(target: str, include_keys: bool = False) -> dict:
    """Write the whole data folder to the zip at `target`. Returns its
    manifest."""
    from core import session_manager_store
    data_dir = _data_dir()
    count = 0
    with tempfile.TemporaryDirectory(prefix="jarvis-backup-") as scratch, \
            zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, rel in _files(data_dir):
            if rel == KEY_FILE and not include_keys:
                continue
            try:
                if _is_sqlite(path):
                    copy = os.path.join(scratch, f"{count}.db")
                    _sqlite_copy(path, copy)
                    archive.write(copy, rel)
                elif rel.endswith(".json") and not include_keys:
                    with open(path, encoding="utf-8") as f:
                        archive.writestr(rel, json.dumps(_blank_secrets(json.load(f)), ensure_ascii=False, indent=2))
                else:
                    archive.write(path, rel)
                count += 1
            except (OSError, ValueError, sqlite3.Error) as e:
                logger.warning("backup: left out %s (%s)", rel, e)
        manifest = {"version": VERSION, "created_at": time.time(), "includes_keys": include_keys, "files": count,
                    "schema_version": session_manager_store.SCHEMA_VERSION}
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
    return manifest


class BackupRefused(ValueError):
    pass


def check_backup(path: str) -> dict:
    """The manifest of a valid backup zip, or BackupRefused saying why."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if MANIFEST not in names:
                raise BackupRefused("this is not a Kairos backup (no manifest)")
            manifest = json.loads(archive.read(MANIFEST))
            if manifest.get("version") != VERSION:
                raise BackupRefused("this backup was made by a version of Kairos this one can't read")
            total = 0
            for info in archive.infolist():
                name = info.filename
                if name.startswith(("/", "\\")) or ":" in name or ".." in name.replace("\\", "/").split("/"):
                    raise BackupRefused(f"the backup names a path outside the data folder: {name}")
                if name.split("/", 1)[0] == RESTORE_DIR:
                    raise BackupRefused("the backup contains the restore area")
                total += info.file_size
            if total > MAX_RESTORE_BYTES:
                raise BackupRefused("the backup is larger than Kairos restores (4 GB)")
            bad = archive.testzip()
            if bad:
                raise BackupRefused(f"the backup is damaged ({bad})")
            return manifest
    except zipfile.BadZipFile:
        raise BackupRefused("this is not a zip file")


def stage_restore(path: str) -> dict:
    """Check a backup and put it in place to be swapped in at the next start."""
    manifest = check_backup(path)
    folder = os.path.join(_data_dir(), RESTORE_DIR)
    os.makedirs(folder, exist_ok=True)
    shutil.copyfile(path, os.path.join(folder, PENDING))
    return manifest


def pending_restore() -> Optional[dict]:
    path = os.path.join(_data_dir(), RESTORE_DIR, PENDING)
    if not os.path.exists(path):
        return None
    try:
        return check_backup(path)
    except BackupRefused:
        return None


def last_restore() -> Optional[dict]:
    try:
        with open(os.path.join(_data_dir(), RESTORE_DIR, LAST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cancel_restore() -> bool:
    path = os.path.join(_data_dir(), RESTORE_DIR, PENDING)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _entries(data_dir: str) -> list[str]:
    """What a restore replaces: everything but the restore area and a vault
    kept inside the data folder (the default one is), which is the person's
    own and never in a backup."""
    vault = _vault_inside(data_dir)
    return [name for name in os.listdir(data_dir) if name != RESTORE_DIR
            and os.path.realpath(os.path.join(data_dir, name)) != vault]


def apply_pending_restore(data_dir: Optional[str] = None) -> Optional[dict]:
    """At start, before anything opens the data folder: swap a staged backup
    in. The current data is copied to .restore/before-restore-<time> first;
    if extracting fails, it is put back. Returns what happened, or None."""
    data_dir = data_dir or _data_dir()
    folder = os.path.join(data_dir, RESTORE_DIR)
    pending = os.path.join(folder, PENDING)
    if not os.path.exists(pending):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safety = os.path.join(folder, f"before-restore-{stamp}")
    result = {"at": time.time(), "safety_copy": safety}
    try:
        manifest = check_backup(pending)
    except BackupRefused as e:
        result.update(ok=False, error=str(e))
        os.replace(pending, os.path.join(folder, f"refused-{stamp}.zip"))
        _write_last(folder, result)
        return result
    os.makedirs(safety)
    for name in _entries(data_dir):
        shutil.move(os.path.join(data_dir, name), os.path.join(safety, name))
    try:
        with zipfile.ZipFile(pending) as archive:
            for info in archive.infolist():
                if info.filename != MANIFEST:
                    archive.extract(info, data_dir)
        # A backup made without keys keeps this install's key, so anything
        # entered again after the restore is encrypted with a key that exists.
        if not manifest.get("includes_keys") and os.path.exists(os.path.join(safety, KEY_FILE)):
            shutil.copy2(os.path.join(safety, KEY_FILE), os.path.join(data_dir, KEY_FILE))
        # Logs and checkpoints are not in a backup; this install's own stay.
        for kept in ("logs", "file_checkpoints"):
            source = os.path.join(safety, kept)
            if os.path.isdir(source) and not os.path.exists(os.path.join(data_dir, kept)):
                shutil.copytree(source, os.path.join(data_dir, kept))
        os.remove(pending)
        result.update(ok=True, backup_created_at=manifest.get("created_at"), includes_keys=manifest.get("includes_keys"))
    except Exception as e:  # put the data back exactly as it was
        for name in _entries(data_dir):
            path = os.path.join(data_dir, name)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        for name in os.listdir(safety):
            shutil.move(os.path.join(safety, name), os.path.join(data_dir, name))
        os.replace(pending, os.path.join(folder, f"failed-{stamp}.zip"))
        result.update(ok=False, error=f"{type(e).__name__}: {e}")
    _write_last(folder, result)
    return result


def _write_last(folder: str, result: dict) -> None:
    with open(os.path.join(folder, LAST), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
