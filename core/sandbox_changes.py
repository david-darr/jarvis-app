"""Change sets: sandbox edits waiting for a person (Hermes phase 7, 2026-09-24).

A run_code call that starts from a copy of the JARVIS code can change files,
but core/sandbox.py throws its copy away. When it does change something,
the edit is kept here as a change set: the new bytes of each added or
modified file, the sha256 each touched file had when it was copied in, the
diff, and the chat it came from. Nothing applies one automatically, and no
model tool can: an admin reviews it in Settings > Administration > Sandbox
changes and applies or discards it there.

Applying is all or nothing. Every target is checked first - the path stays
inside the folder and out of .git, data and .env files, and the file on disk
still has the fingerprint it had when the run copied it (or, for a new file,
still does not exist). One mismatch and nothing is written; the conflict is
named. Then new contents are staged beside their targets and swapped in,
and deletions happen last. Applying does not commit.

Unapplied change sets expire after EXPIRY_DAYS.
"""
import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Optional

from core.constants import BASE_DIR, DATA_DIR

CHANGES_DIR = os.path.join(DATA_DIR, "sandbox_changes")
EXPIRY_DAYS = 7
FORBIDDEN_PARTS = {".git", "data", "node_modules"}


class Conflict(Exception):
    """The folder no longer matches what the run started from."""


def available(root: str = BASE_DIR) -> bool:
    """Change sets only make sense for a development checkout (a folder with
    its .git). In an installed app the folder holds the bundled runtime - a
    copy is far over the sandbox's input limit - and an apply would write
    into files the next update replaces (found 2026-09-25). In a git
    worktree .git is a file pointing at the main repository, and that is
    still a checkout (found 2026-10-05)."""
    return os.path.exists(os.path.join(root, ".git"))


def _path(change_id: str) -> str:
    if not change_id.isalnum():
        raise KeyError(change_id)
    return os.path.join(CHANGES_DIR, f"{change_id}.json")


def _checked_target(root: Path, rel: str) -> Path:
    parts = PurePosixPath(rel).parts
    if (not parts or PurePosixPath(rel).is_absolute() or ".." in parts or ":" in parts[0]
            or any(p in FORBIDDEN_PARTS for p in parts) or parts[-1] == ".env" or parts[-1].startswith(".env.")):
        raise Conflict(f"{rel}: not a path a change set may touch")
    target = (root / rel).resolve()
    if root not in target.parents:
        raise Conflict(f"{rel}: resolves outside the folder")
    return target


def record(result, session_id: Optional[str], root: str = BASE_DIR) -> Optional[str]:
    """Keep a run's edits for review; returns the change set id, or None when
    it changed nothing. `result` is a core/sandbox.SandboxResult."""
    from core.sandbox import SKIPPED_DIRS
    # Build clutter (caches, dependency folders) is not an edit anyone meant.
    changes = [c for c in result.changes if not set(PurePosixPath(c["path"]).parts[:-1]) & SKIPPED_DIRS]
    if not changes:
        return None
    kept = {c["path"] for c in changes}
    expire()
    os.makedirs(CHANGES_DIR, exist_ok=True)
    change_id = uuid.uuid4().hex[:12]
    record = {
        "id": change_id, "root": str(Path(root).resolve()), "session_id": session_id, "created": time.time(),
        "changes": changes, "diff": result.diff,
        "base_hashes": {p: h for p, h in result.base_hashes.items() if p in kept},
        "applicable": result.contents_complete,
        "contents": {p: base64.b64encode(b).decode("ascii") for p, b in result.contents.items() if p in kept},
    }
    with open(_path(change_id), "w", encoding="utf-8") as f:
        json.dump(record, f)
    return change_id


def _load(change_id: str) -> dict:
    try:
        with open(_path(change_id), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise KeyError(change_id)


def _summary(record: dict) -> dict:
    return {k: record[k] for k in ("id", "session_id", "created", "changes", "applicable")}


def list_pending() -> list[dict]:
    expire()
    if not os.path.isdir(CHANGES_DIR):
        return []
    records = []
    for name in os.listdir(CHANGES_DIR):
        if name.endswith(".json"):
            try:
                records.append(_summary(_load(name[:-5])))
            except (KeyError, ValueError):
                continue
    return sorted(records, key=lambda r: r["created"], reverse=True)


def get(change_id: str) -> dict:
    record = _load(change_id)
    return {**_summary(record), "diff": record["diff"]}


def discard(change_id: str) -> None:
    try:
        os.remove(_path(change_id))
    except FileNotFoundError:
        raise KeyError(change_id)


def apply(change_id: str, allowed_root: str = BASE_DIR) -> list[dict]:
    """Write a change set into its folder, all or nothing. Returns the
    changes applied; raises Conflict (nothing written) or KeyError. Only the
    JARVIS folder is ever written, whatever a stored record names."""
    record = _load(change_id)
    if not record.get("applicable"):
        raise Conflict("this change set was too large to keep, so it can only be read")
    root = Path(record["root"])
    if root != Path(allowed_root).resolve():
        raise Conflict("this change set is for a different folder")
    if not available(str(root)):
        raise Conflict("changes can only be applied to a development checkout of JARVIS, not an installed app")
    plan = []
    for change in record["changes"]:
        rel, status = change["path"], change["status"]
        target = _checked_target(root, rel)
        if status == "added":
            if target.exists():
                raise Conflict(f"{rel}: already exists")
        else:
            if not target.is_file():
                raise Conflict(f"{rel}: no longer exists")
            if hashlib.sha256(target.read_bytes()).hexdigest() != record["base_hashes"].get(rel):
                raise Conflict(f"{rel}: changed since the run started")
        plan.append((target, status, None if status == "deleted" else base64.b64decode(record["contents"][rel])))

    staged = []
    try:
        for target, status, data in plan:
            if data is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.jarvis-{change_id}.tmp")
            temp.write_bytes(data)
            staged.append((temp, target))
    except OSError:
        for temp, _ in staged:
            temp.unlink(missing_ok=True)
        raise
    for temp, target in staged:
        os.replace(temp, target)
    for target, status, _ in plan:
        if status == "deleted":
            target.unlink()
    os.remove(_path(change_id))
    return record["changes"]


def expire(now: Optional[float] = None) -> None:
    if not os.path.isdir(CHANGES_DIR):
        return
    cutoff = (now or time.time()) - EXPIRY_DAYS * 86400
    for name in os.listdir(CHANGES_DIR):
        path = os.path.join(CHANGES_DIR, name)
        try:
            if name.endswith(".json") and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            continue
