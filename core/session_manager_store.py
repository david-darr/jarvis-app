"""SQLite storage behind core/session_manager.py's facade — the durable half
of chat session persistence, plus the FTS5 index that makes cross-session
search a real query instead of a substring scan.

Why this replaced the JSON files (2026-09-22)
---------------------------------------------
The previous store wrote each session to its own file and maintained a
separate `sessions_index.json` for listing. That meant every mutation was
TWO independent writes, and `list_sessions`/`get_session`/search all gated
on membership in the index. So a crash — or any second process holding a
stale in-memory index — between the two writes produced a session whose
file existed but which nothing could reach, with no repair path. That was
not hypothetical: the dev data directory had 20 session files against 17
index entries when this was written, three real chats unreachable.

One transaction per mutation removes the failure mode structurally rather
than narrowing the window. There is no longer an index to disagree with.

Why `sessions` keeps a JSON document column
-------------------------------------------
A session dict is not a fixed schema. Fields have accreted with features
(`model_override`, `model_effort`, `context_state`, `artifact_urls`,
`compactions`, `open_mic_started_at`), several of them written by a setter
without ever appearing in create_session()'s initial record. Normalising
that into columns would mean every future field needs a migration, and any
field this module didn't know about would be silently dropped on write.

So the document is the record: `sessions.doc` holds the exact dict callers
already receive from get_session(), and the columns beside it are derived
projections used for ordering, filtering and counting without parsing every
row. `messages` rows are likewise derived, and exist for one reason: FTS5
cannot index inside a JSON blob. Anything the facade reads back comes from
`doc`, so a field added by some unrelated feature tomorrow round-trips
untouched.

Concurrency
-----------
One connection, `check_same_thread=False`, every operation under `_LOCK`.
The backend is a single process, but FastAPI runs handlers across a thread
pool and the Discord channel adapter runs its own loop in-process, so the
lock is load-bearing rather than defensive. WAL mode is set so an external
reader (a debugging shell, a future export script) can read while the app
writes.
"""
import glob
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from typing import Any, Optional

from core.atomic_io import read_json
from core.constants import DATA_DIR

logger = logging.getLogger(__name__)

DB_FILE = os.path.join(DATA_DIR, "sessions.db")

# Legacy JSON locations, read once by migrate_from_json() and then left alone.
LEGACY_SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
LEGACY_INDEX_FILE = os.path.join(DATA_DIR, "sessions_index.json")
LEGACY_CHANNEL_FILE = os.path.join(DATA_DIR, "channel_sessions.json")
LEGACY_BACKUP_DIR = os.path.join(DATA_DIR, "sessions.pre-sqlite-backup")

SCHEMA_VERSION = 1

_LOCK = threading.RLock()

# The columns mirrored out of `doc` on every write. Kept in one place so
# _projection() and the CREATE TABLE below cannot drift apart.
_SESSION_COLUMNS = (
    "title", "starred", "created_at", "updated_at",
    "message_count", "model_endpoint_id", "project_id", "open_mic",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    doc               TEXT NOT NULL,
    title             TEXT,
    starred           INTEGER NOT NULL DEFAULT 0,
    created_at        REAL,
    updated_at        REAL,
    message_count     INTEGER NOT NULL DEFAULT 0,
    model_endpoint_id TEXT,
    project_id        TEXT,
    open_mic          INTEGER NOT NULL DEFAULT 0
);

-- list_sessions() orders starred-first then newest-updated; this is that
-- ordering, so the sidebar's query never sorts in Python.
CREATE INDEX IF NOT EXISTS idx_sessions_order
    ON sessions (starred DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    role       TEXT,
    content    TEXT,
    ts         REAL,
    PRIMARY KEY (session_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id);

-- Contentless-delete FTS: `content=''` would forbid the DELETE we need when
-- a session's messages are rewritten, so this is an ordinary FTS5 table with
-- session_id/role carried UNINDEXED (retrievable, not searchable) so a hit
-- can be attributed without joining back.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5 (
    content,
    session_id UNINDEXED,
    idx        UNINDEXED,
    role       UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS channel_sessions (
    channel_key TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL
);
"""

_conn: Optional[sqlite3.Connection] = None
# Result of the JSON import performed by this process's first connection,
# kept so callers (and tests) can see what the boot-time migration did.
_last_migration: Optional[dict] = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    # Derived from DB_FILE rather than DATA_DIR so the two cannot point at
    # different places if the file location is ever overridden.
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # ON DELETE CASCADE on messages is only honoured with this pragma on, and
    # it is per-connection rather than stored in the file.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    # Assigned BEFORE the migration runs, so the save_session() calls inside
    # it re-enter _connect() and get this same connection instead of
    # recursing into a second one.
    _conn = conn
    _migrate_if_needed(conn)
    return conn


def connection() -> sqlite3.Connection:
    with _LOCK:
        return _connect()


def _projection(doc: dict) -> tuple:
    """The derived column values for a session document. `message_count`
    counts real stored messages rather than trusting a caller-supplied
    number — the old index tracked this separately and was exactly the sort
    of thing that could disagree with the body it described."""
    return (
        doc.get("title"),
        1 if doc.get("starred") else 0,
        doc.get("created_at"),
        doc.get("updated_at"),
        len(doc.get("messages") or []),
        doc.get("model_endpoint_id"),
        doc.get("project_id"),
        1 if doc.get("open_mic") else 0,
    )


def _write_messages(conn: sqlite3.Connection, session_id: str, messages: list) -> None:
    """Rebuild a session's derived message and FTS rows.

    Deliberately a full replace rather than an incremental append: messages
    are not only appended (replace_messages() truncates, compact_session()
    flags rows archived), so an incremental path would need to know which
    kind of edit it was handling. A chat is a few hundred short rows, and
    this runs inside the same transaction as the document write, so the
    derived rows can never describe a different version of the document.
    """
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
    rows = []
    fts_rows = []
    for i, m in enumerate(messages or []):
        content = m.get("content") or ""
        rows.append((session_id, i, m.get("role"), content, m.get("ts")))
        # An archived message stays searchable on purpose: compaction folds
        # it out of what the model is sent, not out of the user's history,
        # and "search my past chats" should still find it.
        fts_rows.append((content, session_id, i, m.get("role")))
    if rows:
        conn.executemany(
            "INSERT INTO messages (session_id, idx, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.executemany(
            "INSERT INTO messages_fts (content, session_id, idx, role) VALUES (?, ?, ?, ?)",
            fts_rows,
        )


def save_session(doc: dict) -> dict:
    """Persist a whole session document and its derived rows in one
    transaction. Every facade mutation funnels through here, which is what
    makes "the listing and the body disagree" unrepresentable."""
    session_id = doc["id"]
    payload = json.dumps(doc, ensure_ascii=False)
    with _LOCK:
        conn = _connect()
        with conn:  # BEGIN/COMMIT, rollback on exception
            conn.execute(
                f"""INSERT INTO sessions (id, doc, {', '.join(_SESSION_COLUMNS)})
                    VALUES (?, ?, {', '.join('?' * len(_SESSION_COLUMNS))})
                    ON CONFLICT(id) DO UPDATE SET
                        doc = excluded.doc,
                        {', '.join(f'{c} = excluded.{c}' for c in _SESSION_COLUMNS)}""",
                (session_id, payload, *_projection(doc)),
            )
            _write_messages(conn, session_id, doc.get("messages") or [])
    return doc


def get_session(session_id: str) -> Optional[dict]:
    with _LOCK:
        conn = _connect()
        row = conn.execute("SELECT doc FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return json.loads(row["doc"]) if row else None


def session_exists(session_id: str) -> bool:
    with _LOCK:
        conn = _connect()
        row = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def list_sessions() -> list[dict]:
    """Sidebar metadata only — never message bodies. Field set matches what
    the old `sessions_index.json` carried, because static/js reads these keys
    by name."""
    with _LOCK:
        conn = _connect()
        rows = conn.execute(
            """SELECT id, title, starred, created_at, updated_at, message_count,
                      model_endpoint_id, project_id, open_mic
               FROM sessions
               ORDER BY starred DESC, updated_at DESC"""
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "starred": bool(r["starred"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "message_count": r["message_count"],
            "model_endpoint_id": r["model_endpoint_id"],
            "project_id": r["project_id"],
            "open_mic": bool(r["open_mic"]),
        }
        for r in rows
    ]


def session_count() -> int:
    with _LOCK:
        conn = _connect()
        return conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]


def delete_session(session_id: str) -> None:
    with _LOCK:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            # messages goes via ON DELETE CASCADE; channel mappings are left
            # to resolve themselves the way they always did — see
            # get_channel_session_id(), which treats a mapping to a missing
            # session as absent rather than erroring.


def delete_all_sessions() -> None:
    """Backs the admin "wipe chats" action."""
    with _LOCK:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM messages_fts")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM channel_sessions")


# ---------------------------------------------------------------- channels

def get_channel_session(channel_key: str) -> Optional[str]:
    with _LOCK:
        conn = _connect()
        row = conn.execute(
            "SELECT session_id FROM channel_sessions WHERE channel_key = ?", (channel_key,)
        ).fetchone()
    return row["session_id"] if row else None


def set_channel_session(channel_key: str, session_id: str) -> None:
    with _LOCK:
        conn = _connect()
        with conn:
            conn.execute(
                """INSERT INTO channel_sessions (channel_key, session_id) VALUES (?, ?)
                   ON CONFLICT(channel_key) DO UPDATE SET session_id = excluded.session_id""",
                (channel_key, session_id),
            )


# ------------------------------------------------------------------ search

def _fts_query(query: str) -> str:
    """Turn a user phrase into an FTS5 MATCH expression.

    Every bare word becomes a quoted term ANDed with the rest, so "swarm
    budget" finds a message containing both words in any order and at any
    distance — the thing the previous substring search could not do. Quoting
    each term is also what keeps FTS5 operators (NOT, NEAR, ^, *, parens)
    in user text from being parsed as syntax, which would otherwise raise
    sqlite3.OperationalError on an ordinary question mid-turn.
    """
    terms = [t for t in "".join(c if c.isalnum() or c in "_'" else " " for c in query).split() if t]
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


def search_messages(query: str, exclude_session_id: Optional[str] = None,
                    max_results: int = 5) -> list[dict]:
    """Ranked full-text search across every stored message.

    Ordered by bm25 relevance rather than recency-of-session, and capped at
    one hit per session so five results mean five different conversations to
    look at rather than five hits in the same one. `snippet()` builds the
    excerpt in SQLite with the matched terms marked by the same "..."
    ellipsis convention the previous Python snippet helper used.
    """
    match = _fts_query(query)
    if not match:
        return []
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT f.session_id, f.role, s.title AS session_title,
                          snippet(messages_fts, 0, '', '', '...', 24) AS snippet,
                          bm25(messages_fts) AS rank
                   FROM messages_fts f
                   JOIN sessions s ON s.id = f.session_id
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT 500""",
                (match,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            # A MATCH expression this module built should always parse; if it
            # somehow doesn't, a failed search must not take down the turn
            # that asked for it.
            logger.error("search_messages: FTS query failed for %r (%s)", query, e)
            return []

    results = []
    seen: set[str] = set()
    for r in rows:
        sid = r["session_id"]
        if sid == exclude_session_id or sid in seen:
            continue
        seen.add(sid)
        results.append({
            "session_id": sid,
            "session_title": r["session_title"],
            "role": r["role"],
            "snippet": r["snippet"],
        })
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------- migration

def _legacy_session_ids() -> list[str]:
    """Every session file present on disk, whichever the old index believed
    in. Globbing rather than reading `sessions_index.json` is the point: a
    file the index had lost is exactly what the JSON store could no longer
    reach, and the migration is the one chance to recover it."""
    return sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(LEGACY_SESSIONS_DIR, "*.json"))
    )


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    """Run the JSON import on the first connection that finds work to do.

    Triggered from _connect() rather than from SessionManager's constructor
    so the migration cannot be missed depending on which module imported
    what first — any code path that touches the store at all gets a migrated
    database. Uses the passed connection directly for its precondition
    checks because the public helpers would re-enter _connect().
    """
    global _last_migration
    if not os.path.isdir(LEGACY_SESSIONS_DIR):
        return
    if not _legacy_session_ids():
        return
    if conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] > 0:
        return
    _last_migration = _import_legacy_json(conn)


def last_migration() -> Optional[dict]:
    """What the boot-time JSON import did, or None if there was nothing to
    import. This is the migration's real entry point in production — nothing
    calls migrate_from_json() directly — so it is also what to assert on."""
    with _LOCK:
        _connect()
        return _last_migration


def migrate_from_json() -> dict:
    """Import the JSON store into SQLite, once.

    Safe to call at any time: it returns immediately unless legacy files are
    present AND the database has no sessions yet, so a partially completed
    run simply repeats from the start rather than double-importing or
    resuming into an inconsistent half-state. The legacy files are MOVED to
    a backup directory and never deleted, so reverting this change means
    moving them back.

    In practice the first _connect() has already done this; the explicit
    call is here for a manual re-run and returns the same shape.
    """
    if not os.path.isdir(LEGACY_SESSIONS_DIR):
        return {"migrated": 0, "skipped": "no legacy sessions directory"}
    if not _legacy_session_ids():
        return {"migrated": 0, "skipped": "no legacy session files"}
    with _LOCK:
        conn = _connect()
        if conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] > 0:
            return {"migrated": 0, "skipped": "database already populated"}
        return _import_legacy_json(conn)


def _import_legacy_json(conn: sqlite3.Connection) -> dict:
    """The import itself. Preconditions are the caller's job."""
    legacy_ids = _legacy_session_ids()
    indexed = set(read_json(LEGACY_INDEX_FILE, {}) or {})
    migrated, recovered, unreadable = 0, [], []

    for sid in legacy_ids:
        path = os.path.join(LEGACY_SESSIONS_DIR, f"{sid}.json")
        doc = read_json(path, None)
        if not isinstance(doc, dict):
            # read_json already logged and preserved a copy. Skip rather than
            # invent a record; the file stays in the backup either way.
            unreadable.append(sid)
            continue
        # A file whose body lost its own id still has one in its filename.
        doc.setdefault("id", sid)
        doc["id"] = doc.get("id") or sid
        doc.setdefault("title", "New Chat")
        doc.setdefault("messages", [])
        now = time.time()
        doc.setdefault("created_at", now)
        doc.setdefault("updated_at", doc.get("created_at", now))
        save_session(doc)
        migrated += 1
        if sid not in indexed:
            recovered.append(sid)

    for channel_key, sid in (read_json(LEGACY_CHANNEL_FILE, {}) or {}).items():
        if isinstance(sid, str):
            set_channel_session(channel_key, sid)

    os.makedirs(LEGACY_BACKUP_DIR, exist_ok=True)
    for src in (LEGACY_SESSIONS_DIR, LEGACY_INDEX_FILE, LEGACY_CHANNEL_FILE):
        if not os.path.exists(src):
            continue
        dest = os.path.join(LEGACY_BACKUP_DIR, os.path.basename(src))
        if os.path.exists(dest):
            # A previous run already parked a copy here. Keep the older
            # backup — it is closer to the pre-migration truth.
            continue
        shutil.move(src, dest)

    result = {
        "migrated": migrated,
        "recovered_orphans": recovered,
        "unreadable": unreadable,
        "backup_dir": LEGACY_BACKUP_DIR,
    }
    logger.info(
        "session store: migrated %d session(s) from JSON to SQLite; "
        "recovered %d not present in the old index (%s); %d unreadable. Backup: %s",
        migrated, len(recovered), ", ".join(recovered) or "none",
        len(unreadable), LEGACY_BACKUP_DIR,
    )
    return result


def rebuild_search_index() -> int:
    """Re-derive every message and FTS row from the stored documents.

    Not used in the normal path — the derived rows are written in the same
    transaction as their document — but a search index is the one thing here
    that can be thrown away and recomputed, so having the recovery exist
    beats needing it later and not having it.
    """
    with _LOCK:
        conn = _connect()
        rows = conn.execute("SELECT doc FROM sessions").fetchall()
        with conn:
            conn.execute("DELETE FROM messages_fts")
            conn.execute("DELETE FROM messages")
            for r in rows:
                doc = json.loads(r["doc"])
                _write_messages(conn, doc["id"], doc.get("messages") or [])
    return len(rows)


def close() -> None:
    """Release the database file. Called at interpreter shutdown in tests so
    a temp data directory can actually be removed — Windows refuses to
    delete a file that still has an open handle."""
    global _conn, _last_migration
    with _LOCK:
        if _conn is not None:
            _conn.close()
        _conn = None
        _last_migration = None


# Test-facing alias; the name says what a caller means by it.
_reset_for_tests = close
