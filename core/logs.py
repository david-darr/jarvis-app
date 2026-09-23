# Log setup and log browsing. The browsing half (reading a tail from the end
# of a large file, relative "since" windows, level/session/component filters)
# is adapted from Hermes Agent's hermes_cli/logs.py, and the per-line session
# tag from hermes_logging.py's record factory, at commit
# 2f6170bfa6 (https://github.com/NousResearch/hermes-agent). JARVIS changes:
# entries are grouped (a traceback stays with the line that raised it, so a
# level or chat filter keeps or drops the whole thing), the tag is a
# ContextVar because JARVIS is async rather than threaded, and following a
# log is a byte cursor a browser can poll rather than a terminal loop.
#
# MIT License
#
# Copyright (c) 2025 Nous Research
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Where JARVIS logs, and reading those logs back (Settings > Admin > Logs).

Three files in DATA_DIR/logs: backend.log (INFO and up, everything),
errors.log (WARNING and up, kept separately so a failure is not rotated out
by routine lines - the "background process died quietly" case), and
desktop.log, written by the Electron shell (electron/desktop-log.js) in the
same line format. Every backend line carries the chat or task it was logged
for, when there is one, so one conversation's trail can be pulled out.
"""
import contextvars
import logging
import os
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional, Sequence

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s%(log_tag)s - %(message)s"

LOG_FILES = {
    "backend": "backend.log",
    "errors": "errors.log",
    "desktop": "desktop.log",
}

# Logger-name prefixes per area, for the component filter.
COMPONENTS = {
    "chat": ("services.chat_service", "core.brain", "core.codex_brain", "core.external_brain", "core.providers",
             "core.session_manager"),
    "swarm": ("core.swarm", "services.swarm_service"),
    "tasks": ("core.task_scheduler", "core.builtin_tasks"),
    "remote": ("core.remote_access", "routes.auth_routes", "uvicorn"),
    "discord": ("core.channels", "discord"),
    "skills": ("services.skills_service", "services.skill_curator"),
}

LEVELS = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}

# The chat or task a line is logged for. A ContextVar, so each asyncio task
# carries its own and concurrent chats never label each other's lines.
_log_tag: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("jarvis_log_tag", default=None)


def set_log_tag(tag: Optional[str]) -> contextvars.Token:
    return _log_tag.set(tag)


def reset_log_tag(token: contextvars.Token) -> None:
    try:
        _log_tag.reset(token)
    except ValueError:
        # Reset from a different context (an async generator finished by
        # another task). Clearing is the correct outcome either way.
        _log_tag.set(None)


def _install_tag_factory() -> None:
    """Give every record a `log_tag`, including third-party ones, so the
    format never fails (Hermes's reason for a factory over a filter)."""
    current = logging.getLogRecordFactory()
    if getattr(current, "_jarvis_log_tag", False):
        return

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        tag = _log_tag.get()
        record.log_tag = f" [{tag}]" if tag else ""
        return record

    factory._jarvis_log_tag = True
    logging.setLogRecordFactory(factory)


_install_tag_factory()


def setup(log_dir: str) -> None:
    """File logging for the backend. Idempotent."""
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    # The root level gates every handler: at Python's default of WARNING,
    # backend.log would silently lose its INFO lines.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    wanted = {"backend.log": (logging.INFO, 2_000_000, 3), "errors.log": (logging.WARNING, 1_000_000, 2)}
    present = {os.path.basename(getattr(h, "baseFilename", "")) for h in root.handlers}
    for filename, (level, max_bytes, backups) in wanted.items():
        if filename in present:
            continue
        handler = RotatingFileHandler(os.path.join(log_dir, filename), maxBytes=max_bytes,
                                      backupCount=backups, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root.addHandler(handler)


# -- reading ------------------------------------------------------------------

# "2026-09-22 21:41:00,123 - core.brain - INFO [chat-id] - message"
_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d+)? - (\S+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL)"
    r"(?: \[([^\]]+)\])? - ")


def parse_since(value: str) -> Optional[datetime]:
    """'30m', '1h', '2d' into a cutoff; None if unparseable."""
    match = re.match(r"^(\d+)\s*([smhd])$", (value or "").strip().lower())
    if not match:
        return None
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    return datetime.now() - timedelta(**{unit: int(match.group(1))})


def _group(lines: Sequence[str]) -> list[dict]:
    """Lines into entries: a header line and any continuation lines under it
    (a traceback). Leading continuation lines, whose header is outside what
    was read, are dropped rather than shown without their context."""
    entries: list[dict] = []
    for line in lines:
        match = _HEADER_RE.match(line)
        if match:
            ts, logger_name, level, tag = match.groups()
            entries.append({"ts": ts, "logger": logger_name, "level": level, "tag": tag,
                            "text": line.rstrip("\r\n")})
        elif entries:
            # \r too: Python's log handlers write \r\n on Windows.
            entries[-1]["text"] += "\n" + line.rstrip("\r\n")
    return entries


def _matches(entry: dict, *, min_level: Optional[str], tag: Optional[str], since: Optional[datetime],
             component: Optional[Sequence[str]], text: Optional[str]) -> bool:
    if min_level and LEVELS[entry["level"]] < LEVELS[min_level]:
        return False
    if tag and entry["tag"] != tag:
        return False
    if since:
        try:
            if datetime.strptime(entry["ts"], "%Y-%m-%d %H:%M:%S") < since:
                return False
        except ValueError:
            pass
    if component and not entry["logger"].startswith(tuple(component)):
        return False
    return not text or text.lower() in entry["text"].lower()


def _read_last_lines(path: str, n: int) -> list[str]:
    """The last n lines; files over 1 MB are read in growing chunks from the
    end rather than whole (Hermes's _read_last_n_lines)."""
    size = os.path.getsize(path)
    if size == 0:
        return []
    if size <= 1_048_576:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    with open(path, "rb") as f:
        chunk_size, pos, lines = 8192, size, []
        while pos > 0 and len(lines) <= n + 1:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk_lines = f.read(read_size).split(b"\n")
            if lines:
                lines[0] = chunk_lines[-1] + lines[0]
                lines = chunk_lines[:-1] + lines
            else:
                lines = chunk_lines
            chunk_size = min(chunk_size * 2, 65536)
    if pos > 0:
        lines = lines[1:]  # the first piece is part of a line that began earlier
    return [raw.decode("utf-8", errors="replace") + "\n" for raw in lines if raw.strip()][-n:]


def _path(name: str, log_dir: str) -> str:
    if name not in LOG_FILES:
        raise ValueError(f"unknown log {name!r}; available: {', '.join(LOG_FILES)}")
    return os.path.join(log_dir, LOG_FILES[name])


def _filters(level: Optional[str], tag: Optional[str], since: Optional[str], component: Optional[str],
             text: Optional[str]) -> dict:
    min_level = level.upper() if level else None
    if min_level and min_level not in LEVELS:
        raise ValueError(f"unknown level {level!r}; use one of {', '.join(LEVELS)}")
    cutoff = None
    if since:
        cutoff = parse_since(since)
        if cutoff is None:
            raise ValueError(f"unknown time window {since!r}; use a form like 30m, 1h or 2d")
    prefixes = None
    if component:
        if component not in COMPONENTS:
            raise ValueError(f"unknown component {component!r}; available: {', '.join(COMPONENTS)}")
        prefixes = COMPONENTS[component]
    return {"min_level": min_level, "tag": tag or None, "since": cutoff, "component": prefixes, "text": text or None}


def tail(name: str, log_dir: str, *, limit: int = 200, level: Optional[str] = None, tag: Optional[str] = None,
         since: Optional[str] = None, component: Optional[str] = None, text: Optional[str] = None) -> dict:
    """The last `limit` matching entries, oldest first, and a cursor (`end`)
    to follow the file from."""
    path = _path(name, log_dir)
    if not os.path.exists(path):
        return {"entries": [], "end": 0, "exists": False}
    end = os.path.getsize(path)
    filters = _filters(level, tag, since, component, text)
    filtered = any(v is not None for v in filters.values())
    # Over-read when filtering so enough entries survive it (Hermes: x20).
    lines = _read_last_lines(path, max(limit * 20, 2000) if filtered else max(limit * 5, 500))
    entries = [e for e in _group(lines) if _matches(e, **filters)][-limit:]
    return {"entries": entries, "end": end, "exists": True}


def follow(name: str, log_dir: str, cursor: int, *, level: Optional[str] = None, tag: Optional[str] = None,
           since: Optional[str] = None, component: Optional[str] = None, text: Optional[str] = None,
           max_bytes: int = 1_048_576) -> dict:
    """Entries written after `cursor`, and the new cursor. A file smaller than
    the cursor has rotated, so reading restarts at its beginning. Only whole
    lines are consumed; a line still being written waits for the next poll."""
    path = _path(name, log_dir)
    if not os.path.exists(path):
        return {"entries": [], "end": 0, "rotated": cursor > 0}
    size = os.path.getsize(path)
    rotated = size < cursor
    start = 0 if rotated else cursor
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(min(size - start, max_bytes))
    whole = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
    lines = whole.decode("utf-8", errors="replace").splitlines(keepends=True)
    filters = _filters(level, tag, since, component, text)
    entries = [e for e in _group(lines) if _matches(e, **filters)]
    return {"entries": entries, "end": start + len(whole), "rotated": rotated}


def list_files(log_dir: str) -> list[dict]:
    files = []
    for name, filename in LOG_FILES.items():
        path = os.path.join(log_dir, filename)
        if os.path.exists(path):
            stat = os.stat(path)
            files.append({"name": name, "file": filename, "size": stat.st_size, "modified": stat.st_mtime})
        else:
            files.append({"name": name, "file": filename, "size": 0, "modified": None})
    return files
