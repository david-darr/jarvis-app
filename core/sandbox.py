"""A contained place to run code (Hermes track, phase 7 step 1, 2026-09-23).

Phase 7's rule is that no model gets a new way to run code, browse or touch
a device until OS-enforced containment is proven. This is that containment:
a throwaway Docker container per run, built on the hardening Hermes Agent's
tools/environments/docker.py uses, and scripts/test_sandbox.py tries to
break out of it. Nothing calls it yet; the tools come in step 2.

What a run gets:
- A copy of its input in /work, the only writable place besides a small
  /tmp. The root filesystem is read-only; no other host path is mounted.
  Symlinks in a copied folder are skipped, so none can point back out.
- No network at all (`--network none`). Opting a run into network access
  needs an egress filter first: on Docker Desktop a networked container can
  reach services on the host's loopback, JARVIS's own API among them.
- No host environment, secrets or Docker socket. It runs as `nobody` with
  every Linux capability dropped and no way to regain privileges.
- Limits on processes, memory (no swap) and CPU, and a deadline enforced
  twice: `timeout` inside the container and `docker kill` from outside.

There is no fallback: when Docker is not running the run is refused, never
done on the host instead.

The changes a run makes come back as a list and a unified diff; the copy is
deleted afterwards, so nothing reaches a real folder unless a caller applies
it.
"""
import asyncio
import difflib
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

logger = logging.getLogger(__name__)

# Pinned by digest, so what runs is exactly what was tested.
IMAGE = "python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 900
MAX_OUTPUT_CHARS = 20_000
MAX_DIFF_CHARS = 40_000
MAX_INPUT_BYTES = 200 * 1024 * 1024
SKIPPED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "data"}
LABEL = "jarvis-sandbox"


class SandboxUnavailable(RuntimeError):
    """Docker is not installed or not running. Never answered by running on the host."""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    changes: list[dict] = field(default_factory=list)  # {"path", "status": added|modified|deleted}
    diff: str = ""


def hardening_args(memory: str, cpus: str, pids: int) -> list[str]:
    """The docker run flags that make the container a sandbox. Kept apart so
    a test can check every one of them is there."""
    return [
        "--network", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--user", "65534:65534",
        "--pids-limit", str(pids),
        "--memory", memory, "--memory-swap", memory,
        "--cpus", cpus,
        "--ipc", "private",
        "--init",
    ]


async def _docker(*args: str, timeout: float = 20) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise SandboxUnavailable("Docker is not installed")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", "docker did not answer"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def available() -> tuple[bool, str]:
    """(True, "") when runs can happen; otherwise (False, why)."""
    try:
        code, out, err = await _docker("info", "--format", "{{.OSType}}")
    except SandboxUnavailable as e:
        return False, str(e)
    if code != 0 or out.strip() != "linux":
        return False, "Docker is not running (start Docker Desktop)"
    return True, ""


def _safe_relative(path: str) -> PurePosixPath:
    rel = PurePosixPath(path.replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or ":" in rel.parts[0]:
        raise ValueError(f"not a path inside the workspace: {path}")
    return rel


def _copy_in(source_dir: str, workspace: Path) -> None:
    total = 0
    root = Path(source_dir)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS and not (here / d).is_symlink()]
        for name in filenames:
            src = here / name
            if src.is_symlink() or not src.is_file():
                continue  # a link could point anywhere on the host
            total += src.stat().st_size
            if total > MAX_INPUT_BYTES:
                raise ValueError("the input folder is larger than the sandbox accepts (200 MB)")
            dest = workspace / src.relative_to(root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


def _snapshot(workspace: Path) -> dict[str, str]:
    files = {}
    for path in workspace.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _read_text(path: Path) -> Optional[list[str]]:
    try:
        if path.stat().st_size > 512 * 1024:
            return None
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (UnicodeDecodeError, OSError):
        return None


def _changes(before: dict[str, str], after: dict[str, str], originals: dict[str, bytes],
             workspace: Path) -> tuple[list[dict], str]:
    changes, diff = [], []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            status = "deleted"
        elif path not in before:
            status = "added"
        elif before[path] != after[path]:
            status = "modified"
        else:
            continue
        changes.append({"path": path, "status": status})
        old = None if status == "added" else _decode(originals.get(path))
        new = None if status == "deleted" else _read_text(workspace / path)
        if (status != "added" and old is None) or (status != "deleted" and new is None):
            diff.append(f"Binary or large file {path} {status}\n")
            continue
        diff.extend(difflib.unified_diff(old or [], new or [], f"a/{path}", f"b/{path}"))
    text = "".join(diff)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n... diff cut here\n"
    return changes, text


def _decode(data: Optional[bytes]) -> Optional[list[str]]:
    if data is None or len(data) > 512 * 1024:
        return None
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _cap(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + "\n... output cut here\n"


async def run(command: str, *, files: Optional[dict[str, str]] = None, source_dir: Optional[str] = None,
              timeout: int = DEFAULT_TIMEOUT_SECONDS, memory: str = "512m", cpus: str = "1",
              pids: int = 128) -> SandboxResult:
    """Run `command` with sh in a fresh container whose /work holds a copy of
    `source_dir` and/or `files` ({relative path: text})."""
    ok, why = await available()
    if not ok:
        raise SandboxUnavailable(why)
    timeout = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
    workspace = Path(tempfile.mkdtemp(prefix="jarvis-sbx-"))
    name = f"jarvis-sbx-{uuid.uuid4().hex[:12]}"
    try:
        if source_dir:
            _copy_in(source_dir, workspace)
        for rel, text in (files or {}).items():
            dest = workspace / _safe_relative(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # newline="": written exactly as given; Windows would turn LF into CRLF.
            dest.write_text(text, encoding="utf-8", newline="")
        before = _snapshot(workspace)
        originals = {p: (workspace / p).read_bytes() for p in before
                     if (workspace / p).stat().st_size <= 512 * 1024}

        args = ["run", "--rm", "--name", name, "--label", LABEL, "--hostname", "sandbox",
                *hardening_args(memory, cpus, pids),
                "-v", f"{workspace}:/work", "-w", "/work", IMAGE,
                "timeout", "-s", "KILL", f"{timeout}s", "sh", "-c", command]
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout + 15)
        except asyncio.TimeoutError:
            timed_out = True
            await _docker("kill", name)
            out, err = await proc.communicate()
        code = proc.returncode if proc.returncode is not None else -1
        stderr = err.decode(errors="replace")
        # 137 is SIGKILL: the inner deadline or the memory limit. Docker
        # removes the container (--rm) before it can be asked which.
        if code == 137:
            stderr += "\n[sandbox] killed: the run hit its time or memory limit\n"
        changes, diff = _changes(before, _snapshot(workspace), originals, workspace)
        return SandboxResult(exit_code=code, stdout=_cap(out.decode(errors="replace")), stderr=_cap(stderr),
                             timed_out=timed_out, changes=changes, diff=diff)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
