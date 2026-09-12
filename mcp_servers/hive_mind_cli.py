"""Hive-mind tool access for Codex, Phase 2 (David's ask 2026-09-11: "all
models no matter what" get notes/tasks/calendar/skills/etc. access).

Originally scoped as a real stdio MCP server (core/hive_mind_server.py's
Claude-side equivalent), matching how Claude and ExternalBrain each reach
the same core/memory_tools.py functions. Live-tested against the real
`codex` CLI before writing this file, and found a hard blocker: `codex
exec` (non-interactive mode) unconditionally requires human approval for
any MCP tool call — "MCP tool call requires approval, but approval policy
is never" — regardless of `approval_policy`, or the `exec_permission_
approvals`/`guardian_approval`/`tool_call_mcp_elicitation`/`mcp_2026_07_28`
feature flags. The only flag that bypasses it, `--dangerously-bypass-
approvals-and-sandbox`, also removes ALL sandboxing (not just MCP
approval) — an unacceptable trade against Phase 1's workspace-write
confinement, and correctly refused when tried. This is a real upstream
CLI limitation, not a config gap.

Pivoted (David's explicit choice) to the mechanism that already works
headlessly with zero approval friction: Codex's native shell tool
(verified extensively in Phase 1). This script is a plain CLI wrapper
around the exact same core/memory_tools.py functions every other model
uses — core/system_prompt.py's for_codex() tells Codex the literal command
to run. Verified live: a workspace-write sandboxed session can read and
execute a script outside its granted cwd/add_dirs without issue (the
sandbox restricts *writes* outside those roots, not reads/execution), so
this file doesn't need to live inside any directory Codex is specially
granted — every Codex session, admin or not, can reach it exactly the way
every other model's hive-mind tools are unconditionally available.

exclude_session_id for search_sessions comes from the JARVIS_CODEX_SESSION_ID
env var (set by core/codex_brain.py when it spawns `codex`), not a CLI flag
— same reasoning as hive_mind_server.py baking it in at server-creation
time rather than trusting the model to supply its own session id correctly.

Output is plain text (same shape as hive_mind_server.py's tool responses),
one result per line where that makes sense — Codex reads this back as
shell stdout, not a structured tool result.

Reads vs. writes take genuinely different paths, found live not assumed:
list_*/search_*/read_* call core/memory_tools.py directly (a plain read of
a JSON file already works fine from inside Codex's workspace-write sandbox
— proven live). create_*/update_*/delete_* do NOT — data/*.json lives
outside the vault/workspace and outside REPO_CODE_DIRS on purpose (it's
also where auth.json and encrypted API keys live), so the sandbox
correctly refuses a direct write there — confirmed live as a clean, fast
"Access is denied", not something to route around with a broader
directory grant. Writes instead call the already-running backend's own
`/api/notes` etc. routes over HTTP (127.0.0.1 only) — a network call, not
a sandboxed filesystem write, so it isn't blocked, and it's the exact same
code path the UI itself uses. Auth is INTERNAL_TOOL_TOKEN (core/auth.py) —
built for exactly this ("app's own tool calls, no browser session") but
unused until now — passed in via the JARVIS_INTERNAL_TOKEN env var (see
core/codex_brain.py) as the X-JARVIS-Internal-Token header every
core/middleware.py route already recognizes; no new auth code needed.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core import memory_tools  # noqa: E402
from core.constants import APP_PORT  # noqa: E402

_API_BASE = f"http://127.0.0.1:{APP_PORT}/api"


def _internal_request(method: str, path: str, json_body: dict | None = None) -> dict:
    token = os.environ.get("JARVIS_INTERNAL_TOKEN", "")
    resp = httpx.request(
        method, f"{_API_BASE}{path}", json=json_body,
        headers={"X-JARVIS-Internal-Token": token}, timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _fmt_notes(notes: list[dict]) -> str:
    return "\n".join(f"- [{n['id']}] {n['text']}" + (f" (due {n['due_date']})" if n.get("due_date") else "") for n in notes) or "No open notes."


def _fmt_tasks(tasks: list[dict]) -> str:
    return "\n".join(f"- [{t['id']}] {t['name']} ({'enabled' if t.get('enabled') else 'disabled'})" for t in tasks) or "No tasks configured."


def _fmt_events(events: list[dict]) -> str:
    return "\n".join(f"- [{e['id']}] {e['title']} ({e['start']})" for e in events) or "Nothing upcoming."


def _fmt_documents(docs: list[dict]) -> str:
    return "\n".join(f"- [{d['id']}] {d['title']}" for d in docs) or "No documents in the Library."


def _fmt_contacts(contacts: list[dict]) -> str:
    return "\n".join(f"- {c['name']}: {c.get('email') or ''} {c.get('phone') or ''}".strip() for c in contacts) or "No contacts synced."


def _fmt_task_runs(runs: list[dict]) -> str:
    return "\n\n".join(f"[{r['task_name']}]: {r['output'] or r.get('error') or '(no output)'}" for r in runs) or "No task runs recorded yet."


def _fmt_skills(skills: list[dict]) -> str:
    return "\n".join(f"- {s['slug']}: {s['description'] or '(no description)'}" for s in skills) or "No skills saved yet."


def main() -> None:
    parser = argparse.ArgumentParser(prog="hive_mind_cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list_notes")
    sub.add_parser("list_tasks")
    sub.add_parser("list_upcoming_events")
    sub.add_parser("list_specs")
    sub.add_parser("list_documents")
    sub.add_parser("list_contacts")
    sub.add_parser("list_task_runs")
    sub.add_parser("list_skills")

    p = sub.add_parser("search_sessions"); p.add_argument("--query", required=True)
    p = sub.add_parser("read_skill"); p.add_argument("--slug", required=True)
    p = sub.add_parser("read_spec"); p.add_argument("--filename", required=True)
    p = sub.add_parser("read_document"); p.add_argument("--doc_id", required=True)

    p = sub.add_parser("create_note")
    p.add_argument("--text", required=True)
    p.add_argument("--due_date")
    p.add_argument("--project", default="personal")

    p = sub.add_parser("update_note")
    p.add_argument("--note_id", required=True)
    p.add_argument("--text"); p.add_argument("--due_date"); p.add_argument("--project")
    p.add_argument("--completed", choices=["true", "false"])

    p = sub.add_parser("delete_note"); p.add_argument("--note_id", required=True)

    p = sub.add_parser("create_task")
    p.add_argument("--name", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--schedule_kind", required=True, choices=["once", "interval", "daily"])
    p.add_argument("--run_at")
    p.add_argument("--interval_seconds", type=int)
    p.add_argument("--run_time")
    p.add_argument("--deliver_to_channel")

    p = sub.add_parser("update_task")
    p.add_argument("--task_id", required=True)
    p.add_argument("--name"); p.add_argument("--prompt")
    p.add_argument("--enabled", choices=["true", "false"])
    p.add_argument("--deliver_to_channel")

    p = sub.add_parser("delete_task"); p.add_argument("--task_id", required=True)

    p = sub.add_parser("create_event")
    p.add_argument("--title", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--all_day", action="store_true")
    p.add_argument("--location", default="")
    p.add_argument("--description", default="")

    p = sub.add_parser("update_event")
    p.add_argument("--event_id", required=True)
    p.add_argument("--title"); p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--location"); p.add_argument("--description")
    p.add_argument("--all_day", choices=["true", "false"])
    p.add_argument("--completed", choices=["true", "false"])

    p = sub.add_parser("delete_event"); p.add_argument("--event_id", required=True)

    args = parser.parse_args()

    try:
        if args.command == "list_notes":
            print(_fmt_notes(memory_tools.list_notes()))
        elif args.command == "list_tasks":
            print(_fmt_tasks(memory_tools.list_tasks()))
        elif args.command == "list_upcoming_events":
            print(_fmt_events(memory_tools.list_upcoming_events()))
        elif args.command == "list_specs":
            print("\n".join(f"- {s}" for s in memory_tools.list_specs()) or "No spec docs found.")
        elif args.command == "list_documents":
            print(_fmt_documents(memory_tools.list_documents()))
        elif args.command == "list_contacts":
            print(_fmt_contacts(memory_tools.list_contacts()))
        elif args.command == "list_task_runs":
            print(_fmt_task_runs(memory_tools.list_task_runs()))
        elif args.command == "list_skills":
            print(_fmt_skills(memory_tools.list_skills()))
        elif args.command == "search_sessions":
            exclude = os.environ.get("JARVIS_CODEX_SESSION_ID") or None
            results = memory_tools.search_sessions(args.query, exclude_session_id=exclude)
            print("\n\n".join(f"[{r['session_title']}] ({r['role']}): {r['snippet']}" for r in results) or "No matches in other sessions.")
        elif args.command == "read_skill":
            print(memory_tools.read_skill(args.slug))
        elif args.command == "read_spec":
            print(memory_tools.read_spec(args.filename))
        elif args.command == "read_document":
            print(memory_tools.read_document(args.doc_id))
        elif args.command == "create_note":
            note = _internal_request("POST", "/notes", {"text": args.text, "due_date": args.due_date, "project": args.project})
            print(f"Created note {note['id']}: {note['text']}")
        elif args.command == "update_note":
            fields = {"text": args.text, "due_date": args.due_date, "project": args.project}
            if args.completed is not None:
                fields["completed"] = args.completed == "true"
            note = _internal_request("PATCH", f"/notes/{args.note_id}", fields)
            print(f"Updated note {note['id']}")
        elif args.command == "delete_note":
            _internal_request("DELETE", f"/notes/{args.note_id}")
            print(f"Deleted note {args.note_id}")
        elif args.command == "create_task":
            task = _internal_request("POST", "/tasks", {
                "name": args.name, "prompt": args.prompt, "schedule_kind": args.schedule_kind,
                "run_at": args.run_at, "interval_seconds": args.interval_seconds,
                "run_time": args.run_time, "deliver_to_channel": args.deliver_to_channel,
            })
            print(f"Created task {task['id']}: {task['name']}")
        elif args.command == "update_task":
            fields = {"name": args.name, "prompt": args.prompt, "deliver_to_channel": args.deliver_to_channel}
            if args.enabled is not None:
                fields["enabled"] = args.enabled == "true"
            task = _internal_request("PATCH", f"/tasks/{args.task_id}", fields)
            print(f"Updated task {task['id']}")
        elif args.command == "delete_task":
            _internal_request("DELETE", f"/tasks/{args.task_id}")
            print(f"Deleted task {args.task_id}")
        elif args.command == "create_event":
            event = _internal_request("POST", "/calendar/events", {
                "title": args.title, "start": args.start, "end": args.end,
                "all_day": args.all_day, "location": args.location, "description": args.description,
            })
            print(f"Created event {event['id']}: {event['title']}")
        elif args.command == "update_event":
            fields = {"title": args.title, "start": args.start, "end": args.end,
                      "location": args.location, "description": args.description}
            if args.all_day is not None:
                fields["all_day"] = args.all_day == "true"
            if args.completed is not None:
                fields["completed"] = args.completed == "true"
            event = _internal_request("PATCH", f"/calendar/events/{args.event_id}", fields)
            print(f"Updated event {event['id']}")
        elif args.command == "delete_event":
            _internal_request("DELETE", f"/calendar/events/{args.event_id}")
            print(f"Deleted event {args.event_id}")
    except (KeyError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"Error: request to {e.request.url} failed — {e.response.status_code} {e.response.text[:300]}", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPError as e:
        print(f"Error: couldn't reach the jarvis-app backend — {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
