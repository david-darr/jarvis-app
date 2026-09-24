"""The vault as a memory backend: a ranked full-text index over its notes.

Replaces a scan that had three defects, measured on David's vault (175 notes)
on 2026-09-23. It was a substring match, so word order mattered ("prompt
cache" found 1 note, "cache prompt" 0); results came in folder order and the
scan stopped at 500 files; and reading cut every note at 4,000 characters,
which silently truncated 111 of the 175 - the same bug the skills had until
the day before. Claude and Codex read the vault with their own file tools and
never went through it; local/API models and Swarm did.

The index is SQLite FTS5, like the chat store (core/session_manager_store.py),
one file per vault in DATA_DIR/memory/. It keeps itself current by comparing
modification times before each search: new and edited notes are re-read,
deleted ones dropped. A note's path and title weigh more than its body, so a
note named for the thing ranks above one that mentions it once.
"""
import hashlib
import logging
import os
import sqlite3
import threading
from typing import Optional

from core.constants import DATA_DIR
from core.memory import MemoryBackend
from core.session_manager_store import _fts_query

logger = logging.getLogger(__name__)

INDEX_DIR = os.path.join(DATA_DIR, "memory")
READ_MAX_CHARS = 60_000
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (path, title, body);
"""
# bm25 column weights: path, title, body. Tuned on David's vault 2026-09-23:
# at 4/6/1 a daily note mentioning "Hermes" and "roadmap" often outranked the
# note named "Hermes-Informed Architecture Roadmap", and "active priorities"
# never found Active Priorities.md first; at 10/20/1 the note named for the
# query leads for every query tried, without burying the rest.
_RANK = "bm25(notes_fts, 10.0, 20.0, 1.0)"


class VaultMemory(MemoryBackend):
    name = "vault"
    _instances: dict[str, "VaultMemory"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def for_dir(cls, vault_dir: str) -> "VaultMemory":
        key = os.path.realpath(vault_dir)
        with cls._instances_lock:
            if key not in cls._instances:
                cls._instances[key] = cls(key)
            return cls._instances[key]

    def __init__(self, vault_dir: str):
        self.vault_dir = os.path.realpath(vault_dir)
        digest = hashlib.sha1(self.vault_dir.lower().encode("utf-8")).hexdigest()[:12]
        self.db_path = os.path.join(INDEX_DIR, f"vault-{digest}.db")
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        os.makedirs(INDEX_DIR, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    def is_available(self) -> bool:
        return os.path.isdir(self.vault_dir)

    def _notes_on_disk(self) -> dict[str, tuple[float, int]]:
        found = {}
        for root, dirs, files in os.walk(self.vault_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for filename in files:
                if filename.endswith(".md"):
                    path = os.path.join(root, filename)
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    found[os.path.relpath(path, self.vault_dir).replace("\\", "/")] = (stat.st_mtime, stat.st_size)
        return found

    def refresh(self, conn: sqlite3.Connection) -> int:
        """Bring the index up to date with the folder; returns notes changed."""
        on_disk = self._notes_on_disk()
        indexed = {r["path"]: (r["mtime"], r["size"]) for r in conn.execute("SELECT path, mtime, size FROM notes")}
        changed = [p for p, meta in on_disk.items() if indexed.get(p) != meta]
        removed = [p for p in indexed if p not in on_disk]
        if not changed and not removed:
            return 0
        with conn:
            for path in removed:
                conn.execute("DELETE FROM notes WHERE path = ?", (path,))
                conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
            for path in changed:
                try:
                    with open(os.path.join(self.vault_dir, path), encoding="utf-8", errors="ignore") as f:
                        body = f.read()
                except OSError:
                    continue
                title = os.path.splitext(os.path.basename(path))[0]
                conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
                conn.execute("INSERT INTO notes_fts (path, title, body) VALUES (?, ?, ?)", (path, title, body))
                conn.execute("INSERT OR REPLACE INTO notes (path, mtime, size) VALUES (?, ?, ?)", (path, *on_disk[path]))
        return len(changed) + len(removed)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Every word must match first; failing that, any word, ranked by how
        many and how rare (as the compacted-chat search does)."""
        if not self.is_available():
            return []
        with self._lock:
            conn = self._connect()
            try:
                self.refresh(conn)
                for joiner in (" AND ", " OR "):
                    match = _fts_query(query, joiner)
                    if not match:
                        return []
                    try:
                        rows = conn.execute(
                            f"""SELECT path, title, snippet(notes_fts, 2, '', '', '...', 24) AS snippet
                                FROM notes_fts WHERE notes_fts MATCH ? ORDER BY {_RANK} LIMIT ?""",
                            (match, limit)).fetchall()
                    except sqlite3.OperationalError as e:
                        logger.error("vault search failed for %r (%s)", query, e)
                        return []
                    if rows:
                        return [{"ref": r["path"], "title": r["title"], "snippet": r["snippet"]} for r in rows]
                return []
            finally:
                conn.close()

    def read(self, ref: str, max_chars: Optional[int] = None) -> str:
        """A note in full, up to max_chars (default READ_MAX_CHARS), saying so
        when it had to cut rather than trailing off. Never outside the vault."""
        max_chars = READ_MAX_CHARS if max_chars is None else max_chars
        full_path = os.path.realpath(os.path.join(self.vault_dir, ref))
        if os.path.commonpath([full_path, self.vault_dir]) != self.vault_dir:
            raise ValueError("path escapes the vault")
        with open(full_path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n\n[This note is {len(text):,} characters; showing the first {max_chars:,}.]"
