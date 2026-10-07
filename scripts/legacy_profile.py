"""An old-format JARVIS data folder for the release check (roadmap phase 8,
2026-10-06; scripts/verify-packaged-app.cjs), and the checks run on it after
each stage. Standard library only, so the packaged runtime can run it.

    python legacy_profile.py make <data dir>
    python legacy_profile.py check <data dir> <stage>   # upgraded | restarted | restored

The folder is the shape a v1.15-era install leaves behind: a session store at
schema v3 with three chats, a task cut off mid-run by a build from before
occurrence claims (no run_source), an MCP server with no status, an
unreadable skill beside a good one, and onboarding finished. Each check
prints one JSON line and exits 1 if anything is wrong.
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

CHATS = 3
LEGACY_TASK = "legacy0task1"


def make(data: str) -> None:
    os.makedirs(data, exist_ok=True)
    db = sqlite3.connect(os.path.join(data, "sessions.db"))
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sessions (id TEXT PRIMARY KEY, doc TEXT NOT NULL, title TEXT, starred INTEGER NOT NULL DEFAULT 0,
            created_at REAL, updated_at REAL, message_count INTEGER NOT NULL DEFAULT 0, model_endpoint_id TEXT,
            project_id TEXT, open_mic INTEGER NOT NULL DEFAULT 0, agent_id TEXT);
        CREATE TABLE messages (session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, idx INTEGER NOT NULL,
            role TEXT, content TEXT, ts REAL, doc TEXT, PRIMARY KEY (session_id, idx));
        CREATE VIRTUAL TABLE messages_fts USING fts5 (content, session_id UNINDEXED, idx UNINDEXED, role UNINDEXED,
            tokenize = 'unicode61');
        CREATE TABLE channel_sessions (channel_key TEXT PRIMARY KEY, session_id TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '3');
        INSERT INTO meta VALUES ('legacy_json_import_completed_at', '1');
    """)
    now = time.time() - 86400
    for n in range(CHATS):
        sid = f"legacychat{n}"
        doc = {"id": sid, "title": f"Old chat {n}", "created_at": now, "updated_at": now + n}
        db.execute("INSERT INTO sessions (id, doc, title, created_at, updated_at, message_count) VALUES (?, ?, ?, ?, ?, 2)",
                   (sid, json.dumps(doc), doc["title"], now, now + n))
        for idx, (role, text) in enumerate((("user", f"Remember the code word ALDER-{n}."), ("assistant", "Noted."))):
            message = {"role": role, "content": text, "ts": now + idx}
            db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", (sid, idx, role, text, now + idx, json.dumps(message)))
            db.execute("INSERT INTO messages_fts VALUES (?, ?, ?, ?)", (text, sid, idx, role))
    db.commit()
    db.close()
    due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    tasks = {LEGACY_TASK: {"id": LEGACY_TASK, "name": "Old hourly report", "prompt": "Summarise", "schedule_kind": "interval",
                           "run_at": None, "interval_seconds": 3600, "run_time": None, "enabled": True,
                           "next_run_at": due, "last_run_at": None, "created_at": now, "builtin_action": None,
                           "deliver_to_channel": None, "endpoint_id": None, "agent_id": None, "report_when": None,
                           "trigger": None, "run_started_at": time.time() - 120}}
    for name, value in (("tasks.json", tasks), ("task_runs.json", []),
                        ("settings.json", {"onboarding_complete": True}),
                        ("integrations.json", {"oldmcp000001": {"id": "oldmcp000001", "kind": "mcp_server", "name": "Old server",
                                                                "mcp_type": "http", "url": "http://127.0.0.1:9/mcp",
                                                                "command": None, "args": [], "api_key_encrypted": None}})):
        with open(os.path.join(data, name), "w", encoding="utf-8") as f:
            json.dump(value, f)
    for slug, body in (("good-old-skill", b"---\ndescription: An old skill\n---\n\nStep one.\n"),
                       ("broken-old-skill", b"\xff\xfe\xfa not text")):
        os.makedirs(os.path.join(data, "skills", slug), exist_ok=True)
        with open(os.path.join(data, "skills", slug, "SKILL.md"), "wb") as f:
            f.write(body)


def facts(data: str) -> dict:
    db = sqlite3.connect(f"file:{os.path.join(data, 'sessions.db')}?mode=ro", uri=True)
    try:
        version = int(db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
        chats = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        words = db.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'ALDER'").fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        db.close()
    with open(os.path.join(data, "task_runs.json"), encoding="utf-8") as f:
        runs = [r for r in json.load(f) if r.get("task_id") == LEGACY_TASK]
    with open(os.path.join(data, "tasks.json"), encoding="utf-8") as f:
        task = json.load(f).get(LEGACY_TASK, {})
    snapshots = sorted(n for n in os.listdir(data) if n.startswith("sessions.db.pre-v"))
    return {"schema_version": version, "chats": chats, "searchable_messages": words,
            "has_new_tables": {"runs", "deliveries", "helpers", "run_events"} <= tables, "snapshots": snapshots,
            "legacy_task_runs": [r.get("outcome") for r in runs], "legacy_task_still_marked": "run_started_at" in task,
            "legacy_task_next_run_at": task.get("next_run_at")}


def check(data: str, stage: str) -> int:
    found = facts(data)
    problems = []
    if found["chats"] != CHATS or found["searchable_messages"] != CHATS:
        problems.append("chats or their search index did not survive")
    if not found["has_new_tables"]:
        problems.append("the session store was not upgraded")
    if found["snapshots"] != ["sessions.db.pre-v3"] and stage != "restored":
        problems.append(f"expected exactly the pre-v3 copy, found {found['snapshots']}")
    if found["legacy_task_runs"] != ["lost"]:
        problems.append(f"the cut-off task should be recorded lost once and never rerun: {found['legacy_task_runs']}")
    if found["legacy_task_still_marked"]:
        problems.append("the cut-off task is still marked as running")
    if stage == "restored":
        with open(os.path.join(data, ".restore", "last.json"), encoding="utf-8") as f:
            last = json.load(f)
        found["restore"] = last
        if not last.get("ok") or not os.path.isdir(last.get("safety_copy", "")):
            problems.append("the restore did not finish with a safety copy")
    found["problems"] = problems
    found["stage"] = stage
    print(json.dumps(found))
    return 1 if problems else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["make"]:
        make(sys.argv[2])
    elif sys.argv[1:2] == ["check"]:
        sys.exit(check(sys.argv[2], sys.argv[3]))
    else:
        sys.exit("usage: legacy_profile.py make <data dir> | check <data dir> <stage>")
