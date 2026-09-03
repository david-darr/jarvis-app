"""Brain-interface-compatible wrapper around a registered "bring your own
model" endpoint (core/model_endpoints.py) — connect()/run_turn()/
run_turn_stream()/disconnect(), same shape as core/brain.py's Brain, so
services/chat_service.py can pick either implementation per session without
its own call sites caring which one they're talking to.

Unlike Brain (which delegates all conversation state to the Claude Agent
SDK's own connection), a plain OpenAI-compatible endpoint is stateless per
request — this class holds the running message list itself, seeded from the
session's already-persisted history on connect() so a pinned-model session
picks up mid-conversation correctly (e.g. after a server restart).

Shared memory + cross-session awareness (David's ask 2026-08-31, "out of
the box for all imported AI models both local and API") — this is the
non-Claude half. Claude already has native vault file access; a plain
OpenAI-compatible model has none at all today, so it gets search_vault,
read_vault_file, and search_sessions as real function-calling tools (see
core/providers/openai_compatible.py's tool-calling loop). Not every model
actually supports tool calling — that's a per-endpoint capability, degraded
gracefully in the provider client, not assumed here.

list_skills/read_skill added 2026-09-01 (David's ask: "all models... know,
utilize, and operate under jarvis's methods, skills, and memory, all as a
hive mind") — same reasoning, same shared engine (core/memory_tools.py).
"""
from typing import AsyncIterator

from core import memory_tools, system_prompt
from core.providers import openai_compatible

_MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_vault",
            "description": "Search JARVIS's memory (the Obsidian vault) for notes matching a keyword or phrase. Returns short snippets, not full files.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Keyword or phrase to search for"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_vault_file",
            "description": "Read one specific vault note in full, by its relative path (as returned by search_vault).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path within the vault, e.g. 'Active Priorities.md'"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_sessions",
            "description": "Search across every other chat session's message history for a keyword or phrase — recall something discussed elsewhere. Returns short snippets, not full histories.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Keyword or phrase to search for"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List every available Skill (a portable, saved procedure for how to do something) by name and one-line description.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Read one Skill's full procedure by its slug (from list_skills).",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string", "description": "The skill's slug, from list_skills"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List open (not-yet-completed) Notes — todos, reminders, priorities. Use for \"what do I need to do\" / priorities questions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List scheduled/automated Tasks (recurring or one-shot jobs JARVIS runs on its own) — distinct from Notes' todos.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upcoming_events",
            "description": "List upcoming Calendar events and due-dated Notes for the next 14 days. Use for \"what's coming up\" / schedule questions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_specs",
            "description": "List every architecture/subsystem spec doc — real documentation about how JARVIS itself is built.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_spec",
            "description": "Read one spec doc in full by filename (from list_specs).",
            "parameters": {
                "type": "object",
                "properties": {"filename": {"type": "string", "description": "The spec's filename, from list_specs"}},
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List every document in the Library (title/tags only, not content).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read one Library document's full content by id (from list_documents).",
            "parameters": {
                "type": "object",
                "properties": {"doc_id": {"type": "string", "description": "The document's id, from list_documents"}},
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "List synced contacts (name/email/phone).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_task_runs",
            "description": "List recent Task execution history — what a scheduled/automated Task actually produced when it last ran.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # -- Notes/Tasks/Calendar writes (David's ask 2026-09-01) — same
    # service-layer functions the app's own routes use, see
    # memory_tools.py's comment on why raw data/ file access isn't used.
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a new Note (a todo/reminder/priority item).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "due_date": {"type": "string", "description": "Optional ISO 8601 datetime"},
                    "project": {"type": "string", "description": "Defaults to 'personal' if omitted"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_note",
            "description": "Update an existing Note by id (from list_notes) — only pass fields to change. Use completed=true to mark it done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string"},
                    "text": {"type": "string"},
                    "due_date": {"type": "string"},
                    "project": {"type": "string"},
                    "completed": {"type": "boolean"},
                },
                "required": ["note_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Delete a Note by id (from list_notes). Irreversible.",
            "parameters": {"type": "object", "properties": {"note_id": {"type": "string"}}, "required": ["note_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new scheduled/automated Task. schedule_kind is 'once' (needs run_at) or 'interval' (needs interval_seconds).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "schedule_kind": {"type": "string", "enum": ["once", "interval"]},
                    "run_at": {"type": "string", "description": "ISO 8601 datetime, for schedule_kind='once'"},
                    "interval_seconds": {"type": "integer", "description": "For schedule_kind='interval'"},
                    "deliver_to_channel": {"type": "string"},
                },
                "required": ["name", "prompt", "schedule_kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": "Update an existing Task by id (from list_tasks) — only pass fields to change. Use enabled=false to pause it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "deliver_to_channel": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Delete a Task by id (from list_tasks). Irreversible.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create a new Calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO 8601 datetime"},
                    "end": {"type": "string", "description": "ISO 8601 datetime"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": "Update an existing Calendar event by id (from list_upcoming_events) — only pass fields to change. Use completed=true to check it off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "completed": {"type": "boolean"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Delete a Calendar event by id (from list_upcoming_events). Irreversible.",
            "parameters": {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]},
        },
    },
    # -- Repo dev access (David's ask 2026-09-01: full read/write on
    # jarvis-app's own source, to work on developmental projects). Claude
    # gets this natively via its own file tools (see core/brain.py's
    # add_dirs); a plain OpenAI-compatible model has no native file access
    # at all, so it gets the equivalent as real tools instead.
    {
        "type": "function",
        "function": {
            "name": "list_repo_directory",
            "description": "List one level of jarvis-app's own source tree (not recursive — call again with a sub-path to descend). Omit path to list the top-level accessible directories.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_repo_file",
            "description": "Read one file from jarvis-app's own source, by path relative to the repo root (e.g. 'core/brain.py').",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_repo_file",
            "description": "Create or overwrite one file in jarvis-app's own source with the given full content (full-file replacement, not a patch/diff). Creates parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
]

# Shell execution (David's ask 2026-09-02, modeled on Odysseus's own
# agent-tool gating) — admin-only, appended to a session's tool list only
# when is_admin is True (see __init__ below), so a non-admin session never
# even sees this tool exists. No command blocklist, matching Odysseus's
# actual safety model — the admin gate is the whole boundary.
_SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a shell command in jarvis-app's own repo root (or a given cwd). Use this to verify/run code you just wrote, e.g. a compile check or a test.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "Optional, defaults to the jarvis-app repo root"},
            },
            "required": ["command"],
        },
    },
}


class ExternalBrain:
    def __init__(self, base_url: str, model: str, api_key: str | None, history: list[dict] | None = None,
                 session_id: str | None = None, num_ctx: int | None = None, is_admin: bool = False):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.session_id = session_id
        self.num_ctx = num_ctx
        self.tools = _MEMORY_TOOLS + [_SHELL_TOOL] if is_admin else _MEMORY_TOOLS
        # The "landing zone" (David's ask 2026-09-01, after live-testing
        # found chats couldn't answer real vault/memory questions) — a
        # real system message, not just tool descriptions, so a model
        # knows memory/skills exist and where to start looking, same
        # pointer-not-a-dump role core/brain.py's system_prompt plays for
        # Claude. Only prepended once, on a session with no prior history —
        # an existing conversation already carries its own system message
        # from when it was first created.
        seeded = [{"role": m["role"], "content": m["content"]} for m in (history or [])]
        if not seeded or seeded[0].get("role") != "system":
            seeded.insert(0, {"role": "system", "content": system_prompt.for_external(is_admin)})
        self._messages: list[dict] = seeded
        # Set on every completed turn that reported usage (David's ask
        # 2026-09-01, per-model token usage on Home) — best-effort, since
        # not every OpenAI-compatible endpoint returns it. Read by
        # services/chat_service.py right after run_turn()/run_turn_stream().
        self.last_usage: dict | None = None

    async def _execute_tool(self, name: str, args: dict) -> str:
        try:
            if name == "run_shell":
                result = await memory_tools.run_shell(args.get("command", ""), cwd=args.get("cwd"))
                return f"exit_code={result['exit_code']}\nstdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"
            if name == "search_vault":
                results = memory_tools.search_vault(args.get("query", ""))
                return "\n\n".join(f"[{r['path']}]: {r['snippet']}" for r in results) or "No matches in the vault."
            if name == "read_vault_file":
                return memory_tools.read_vault_file(args.get("path", ""))
            if name == "search_sessions":
                results = memory_tools.search_sessions(args.get("query", ""), exclude_session_id=self.session_id)
                return "\n\n".join(f"[{r['session_title']}] ({r['role']}): {r['snippet']}" for r in results) or "No matches in other sessions."
            if name == "list_skills":
                skills = memory_tools.list_skills()
                return "\n".join(f"- {s['slug']}: {s['description'] or '(no description)'}" for s in skills) or "No skills saved yet."
            if name == "read_skill":
                return memory_tools.read_skill(args.get("slug", ""))
            if name == "list_notes":
                notes = memory_tools.list_notes()
                return "\n".join(f"- {n['text']}" + (f" (due {n['due_date']})" if n.get("due_date") else "") for n in notes) or "No open notes."
            if name == "list_tasks":
                tasks = memory_tools.list_tasks()
                return "\n".join(f"- {t['name']} ({'enabled' if t.get('enabled') else 'disabled'})" for t in tasks) or "No tasks configured."
            if name == "list_upcoming_events":
                events = memory_tools.list_upcoming_events()
                return "\n".join(f"- {e['title']} ({e['start']})" for e in events) or "Nothing upcoming in the next 14 days."
            if name == "list_specs":
                specs = memory_tools.list_specs()
                return "\n".join(f"- {s}" for s in specs) or "No spec docs found."
            if name == "read_spec":
                return memory_tools.read_spec(args.get("filename", ""))
            if name == "list_documents":
                docs = memory_tools.list_documents()
                return "\n".join(f"- {d['id']}: {d['title']}" for d in docs) or "No documents in the Library."
            if name == "read_document":
                return memory_tools.read_document(args.get("doc_id", ""))
            if name == "list_contacts":
                contacts = memory_tools.list_contacts()
                return "\n".join(f"- {c['name']}: {c.get('email') or ''} {c.get('phone') or ''}".strip() for c in contacts) or "No contacts synced."
            if name == "list_task_runs":
                runs = memory_tools.list_task_runs()
                return "\n\n".join(f"[{r['task_name']}]: {r['output'] or r.get('error') or '(no output)'}" for r in runs) or "No task runs recorded yet."
            if name == "create_note":
                note = memory_tools.create_note(args["text"], due_date=args.get("due_date"), project=args.get("project", "personal"))
                return f"Created note {note['id']}: {note['text']}"
            if name == "update_note":
                note_id = args["note_id"]
                fields = {k: v for k, v in args.items() if k != "note_id" and v is not None}
                note = memory_tools.update_note(note_id, **fields)
                return f"Updated note {note['id']}"
            if name == "delete_note":
                memory_tools.delete_note(args["note_id"])
                return f"Deleted note {args['note_id']}"
            if name == "create_task":
                task = memory_tools.create_task(
                    args["name"], args["prompt"], args["schedule_kind"],
                    run_at=args.get("run_at"), interval_seconds=args.get("interval_seconds"),
                    deliver_to_channel=args.get("deliver_to_channel"),
                )
                return f"Created task {task['id']}: {task['name']}"
            if name == "update_task":
                task_id = args["task_id"]
                fields = {k: v for k, v in args.items() if k != "task_id" and v is not None}
                task = memory_tools.update_task(task_id, **fields)
                return f"Updated task {task['id']}"
            if name == "delete_task":
                memory_tools.delete_task(args["task_id"])
                return f"Deleted task {args['task_id']}"
            if name == "create_event":
                event = memory_tools.create_event(
                    args["title"], args["start"], args["end"], all_day=args.get("all_day", False),
                    location=args.get("location", ""), description=args.get("description", ""),
                )
                return f"Created event {event['id']}: {event['title']}"
            if name == "update_event":
                event_id = args["event_id"]
                fields = {k: v for k, v in args.items() if k != "event_id" and v is not None}
                event = memory_tools.update_event(event_id, **fields)
                return f"Updated event {event['id']}"
            if name == "delete_event":
                memory_tools.delete_event(args["event_id"])
                return f"Deleted event {args['event_id']}"
            if name == "list_repo_directory":
                entries = memory_tools.list_repo_directory(args.get("path", ""))
                return "\n".join(entries) or "(empty)"
            if name == "read_repo_file":
                return memory_tools.read_repo_file(args["path"])
            if name == "write_repo_file":
                return memory_tools.write_repo_file(args["path"], args["content"])
            return f"Unknown tool: {name}"
        except Exception as e:
            return f"Tool error: {e}"

    async def connect(self) -> None:
        pass  # stateless HTTP calls — nothing to open ahead of time

    async def run_turn(self, user_text: str) -> str:
        self._messages.append({"role": "user", "content": user_text})
        reply = await openai_compatible.run_turn(
            self.base_url, self.model, self.api_key, self._messages,
            tools=self.tools, tool_executor=self._execute_tool,
            on_usage=lambda u: setattr(self, "last_usage", u), num_ctx=self.num_ctx,
        )
        self._messages.append({"role": "assistant", "content": reply})
        return reply

    async def run_turn_stream(self, user_text: str) -> AsyncIterator[str]:
        self._messages.append({"role": "user", "content": user_text})
        parts: list[str] = []
        async for chunk in openai_compatible.run_turn_stream(
            self.base_url, self.model, self.api_key, self._messages,
            tools=self.tools, tool_executor=self._execute_tool,
            on_usage=lambda u: setattr(self, "last_usage", u), num_ctx=self.num_ctx,
        ):
            parts.append(chunk)
            yield chunk
        self._messages.append({"role": "assistant", "content": "".join(parts)})

    async def disconnect(self) -> None:
        pass
