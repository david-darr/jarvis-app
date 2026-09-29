"""Git object checkpoints around model turns, independent of a user's Git repo.

Each watched root has a bare object store in JARVIS data. A turn records a
before commit before the model starts and an after commit even if it fails.
No Git executable is needed in the packaged app.
"""
import asyncio
import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import zlib
from contextlib import asynccontextmanager

from core.constants import BASE_DIR, DATA_DIR
from core.vault import resolve_vault_dir

logger = logging.getLogger(__name__)
STORE = Path(DATA_DIR) / "file_checkpoints"
MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist"}


class Conflict(Exception):
    pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        name = tmp.name
    os.replace(name, path)


def _repo(root: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
    repo = STORE / "repos" / key
    if not (repo / "HEAD").exists():
        _atomic_write(repo / "HEAD", b"ref: refs/heads/checkpoints\n")
        _atomic_write(repo / "config", b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
    return repo


def _object(repo: Path, kind: str, body: bytes) -> str:
    raw = f"{kind} {len(body)}\0".encode() + body
    oid = hashlib.sha1(raw).hexdigest()
    target = repo / "objects" / oid[:2] / oid[2:]
    if not target.exists():
        _atomic_write(target, zlib.compress(raw))
    return oid


def _read_object(repo: Path, oid: str) -> tuple[str, bytes]:
    if len(oid) != 40 or any(c not in "0123456789abcdef" for c in oid):
        raise ValueError("invalid object id")
    raw = zlib.decompress((repo / "objects" / oid[:2] / oid[2:]).read_bytes())
    header, body = raw.split(b"\0", 1)
    kind, size = header.split(b" ", 1)
    if len(body) != int(size) or hashlib.sha1(raw).hexdigest() != oid:
        raise ValueError("corrupt checkpoint object")
    return kind.decode(), body


def _git_paths(root: Path) -> list[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                cwd=root, capture_output=True, timeout=20, check=True)
        return [os.fsdecode(p) for p in result.stdout.split(b"\0") if p]
    except (OSError, subprocess.SubprocessError):
        return None


def _files(root: Path):
    listed = _git_paths(root)
    if listed is not None:
        for name in listed:
            if not any(part in SKIP_DIRS for part in Path(name).parts):
                yield name
        return
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not (Path(folder) / d).is_symlink()
                   and not (root == Path(BASE_DIR).resolve() and
                            ((Path(folder) == root and d == "data") or
                             (Path(folder) == root / "electron" and d == "runtime")))
                   and not (Path(folder) / d).resolve().is_relative_to(STORE.resolve())]
        for file in files:
            if file == ".git":
                continue
            path = Path(folder) / file
            if not path.is_symlink() and not path.is_dir():
                yield path.relative_to(root).as_posix()


def _snapshot(root: Path) -> tuple[str, dict[str, str], list[str]]:
    repo = _repo(root)
    manifest: dict[str, str] = {}
    skipped: list[str] = []
    total = 0
    for name in _files(root):
        path = root / name
        try:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            size = path.stat().st_size
            if size > MAX_FILE or total + size > MAX_TOTAL:
                skipped.append(name)
                continue
            body = path.read_bytes()
            total += len(body)
            manifest[name.replace("\\", "/")] = _object(repo, "blob", body)
        except (OSError, ValueError) as exc:
            skipped.append(name)
            logger.warning("checkpoint skipped %s: %s", path, exc)

    def tree(items: dict[str, str]) -> str:
        entries: dict[str, dict[str, str] | str] = {}
        for name, oid in items.items():
            parts = name.split("/")
            branch = entries
            for part in parts[:-1]:
                branch = branch.setdefault(part, {})  # type: ignore[assignment]
            branch[parts[-1]] = oid
        def encode(branch: dict) -> str:
            out = bytearray()
            for name, value in sorted(branch.items(), key=lambda pair: pair[0].encode() + (b"/" if isinstance(pair[1], dict) else b"")):
                is_dir = isinstance(value, dict)
                child = encode(value) if is_dir else value
                out.extend(("40000" if is_dir else "100644").encode() + b" " + name.encode() + b"\0" + bytes.fromhex(child))
            return _object(repo, "tree", bytes(out))
        return encode(entries)

    return tree(manifest), manifest, skipped


def _commit(repo: Path, tree_oid: str, event_id: str, phase: str) -> str:
    stamp = int(time.time())
    body = (f"tree {tree_oid}\nauthor JARVIS <jarvis@localhost> {stamp} +0000\n"
            f"committer JARVIS <jarvis@localhost> {stamp} +0000\n\n"
            f"{event_id} {phase}\n").encode()
    oid = _object(repo, "commit", body)
    _atomic_write(repo / "refs" / "checkpoints" / event_id / phase, f"{oid}\n".encode())
    return oid


def _event_path(event_id: str) -> Path:
    if len(event_id) != 32 or any(c not in "0123456789abcdef" for c in event_id):
        raise KeyError(event_id)
    return STORE / "events" / f"{event_id}.json"


def _save(event: dict) -> None:
    _atomic_write(_event_path(event["id"]), json.dumps(event, ensure_ascii=False).encode())


def _roots(workspace: str | None = None) -> list[Path]:
    roots = [Path(resolve_vault_dir()).resolve()]
    source_root = Path(BASE_DIR).resolve()
    if (source_root / ".git").exists() and source_root not in roots:
        roots.append(source_root)
    if workspace:
        folder = Path(workspace).resolve()
        if folder not in roots:
            roots.append(folder)
    return [root for root in roots if root.is_dir()]


def _start(source: str, workspace: str | None) -> dict:
    event = {"id": uuid.uuid4().hex, "created": time.time(), "source": source,
             "status": "running", "roots": []}
    for root in _roots(workspace):
        tree, manifest, skipped = _snapshot(root)
        repo = _repo(root)
        event["roots"].append({"path": str(root), "before": _commit(repo, tree, event["id"], "before"),
                               "before_files": manifest, "before_skipped": skipped})
    _save(event)
    return event


def _finish(event: dict) -> None:
    changed = False
    for root_info in event["roots"]:
        root = Path(root_info["path"])
        tree, manifest, skipped = _snapshot(root)
        root_info["after"] = _commit(_repo(root), tree, event["id"], "after")
        before = root_info.pop("before_files")
        root_info["created_unprotected"] = [name for name in skipped
                                             if name not in before and name not in root_info["before_skipped"]]
        unavailable = set(root_info["before_skipped"]) | set(skipped)
        root_info["changes"] = [{"path": name, "before": before.get(name), "after": manifest.get(name)}
                                for name in sorted(before.keys() | manifest.keys())
                                if name not in unavailable and before.get(name) != manifest.get(name)]
        root_info["after_skipped"] = skipped
        changed |= bool(root_info["changes"] or root_info["created_unprotected"])
    event["status"] = "changed" if changed else "unchanged"
    event["finished"] = time.time()
    _save(event)
    if changed and event["source"].startswith("chat:"):
        try:
            from core import chat_files
            chat_files.record_checkpoint(event)
        except Exception:
            logger.exception("could not index files from checkpoint %s", event["id"])


@asynccontextmanager
async def around_turn(source: str, workspace: str | None = None):
    """Snapshot both roots without making a checkpoint failure break a chat."""
    event = None
    try:
        event = await asyncio.to_thread(_start, source, workspace)
    except Exception:
        logger.exception("could not start file checkpoint for %s", source)
    try:
        yield
    finally:
        if event is not None:
            try:
                finish = asyncio.create_task(asyncio.to_thread(_finish, event))
                try:
                    await asyncio.shield(finish)
                except asyncio.CancelledError:
                    await finish
                    raise
            except Exception:
                logger.exception("could not finish file checkpoint %s", event["id"])


def list_events(limit: int = 100) -> list[dict]:
    events = []
    for path in (STORE / "events").glob("*.json"):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            events.append({"id": event["id"], "created": event["created"], "finished": event.get("finished"),
                           "source": event["source"],
                           "status": event["status"], "roots": [{"path": r["path"], "changes": r.get("changes", []),
                           "skipped": sorted(set(r.get("before_skipped", [])) | set(r.get("after_skipped", [])))}
                           for r in event["roots"]]})
        except (OSError, ValueError, KeyError):
            logger.warning("invalid checkpoint record %s", path)
    recent = sorted(events, key=lambda e: e["created"], reverse=True)[:limit]
    for event in recent:
        own_roots = {r["path"] for r in event["roots"]}
        event["overlap"] = any(other["id"] != event["id"]
                               and own_roots.intersection(r["path"] for r in other["roots"])
                               and event["created"] < (other["finished"] or float("inf"))
                               and other["created"] < (event["finished"] or float("inf"))
                               for other in events)
    return recent


def _load_event(event_id: str) -> dict:
    try:
        return json.loads(_event_path(event_id).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KeyError(event_id) from exc


def _current_blob_oid(root: Path, name: str) -> str | None:
    path = root / name
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path, *path.parents] if p != root):
        raise Conflict(f"Unsafe path: {name}")
    if not path.exists():
        return None
    if not path.is_file():
        raise Conflict(f"Path is no longer a file: {name}")
    body = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()


def get_event(event_id: str) -> dict:
    event = _load_event(event_id)
    for root_info in event["roots"]:
        root = Path(root_info["path"]).resolve()
        repo = _repo(root)
        for change in root_info.get("changes", []):
            try:
                current = _current_blob_oid(root, change["path"])
                change["restore_state"] = ("restored" if change.get("restored_at") else
                                           "available" if current == change["after"] else
                                           "at_before" if current == change["before"] else "changed_since")
            except (OSError, Conflict):
                change["restore_state"] = "changed_since"
            old = _read_object(repo, change["before"])[1] if change["before"] else b""
            new = _read_object(repo, change["after"])[1] if change["after"] else b""
            try:
                if max(len(old), len(new)) > 1024 * 1024:
                    change["diff"] = "File is too large to show a text diff."
                    continue
                a, b = old.decode("utf-8"), new.decode("utf-8")
                change["diff"] = "".join(list(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                 fromfile="before/" + change["path"], tofile="after/" + change["path"]))[:2000])[:100000]
            except UnicodeDecodeError:
                change["diff"] = "Binary file changed."
    return event


def restore(event_id: str, selections: list[dict]) -> list[dict]:
    """Restore selected paths only when each still equals its after image."""
    event = _load_event(event_id)
    if event["status"] != "changed":
        raise Conflict("This checkpoint has no completed changes.")
    chosen = []
    seen = set()
    for selection in selections:
        root_name, name = selection.get("root"), selection.get("path")
        if (root_name, name) in seen:
            raise Conflict("A file was selected twice.")
        seen.add((root_name, name))
        root_info = next((r for r in event["roots"] if r["path"] == root_name), None)
        change = next((c for c in (root_info or {}).get("changes", []) if c["path"] == name), None)
        if not root_info or not change:
            raise Conflict("Selected file is not in this checkpoint.")
        if change.get("restored_at"):
            raise Conflict(f"Already restored: {name}")
        root = Path(root_name).resolve()
        path = root / name
        repo = _repo(root)
        current = _current_blob_oid(root, name)
        if current != change["after"]:
            if current == change["before"]:
                raise Conflict(f"Already at previous state: {name}")
            raise Conflict(f"Changed since checkpoint: {name}")
        old = _read_object(repo, change["before"])[1] if change["before"] else None
        chosen.append((path, old, selection, change))
    for path, old, _, _ in chosen:
        if old is None:
            path.unlink()
        else:
            _atomic_write(path, old)
    for _, _, _, change in chosen:
        change["restored_at"] = time.time()
    try:
        _save(event)
    except OSError:
        logger.exception("files restored but checkpoint %s status could not be saved", event_id)
    return [selection for _, _, selection, _ in chosen]
