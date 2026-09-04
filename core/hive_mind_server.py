"""Claude's half of shared memory + cross-session awareness (David's ask
2026-08-31). Claude already has native file-tool access to the vault (its
cwd) — the only real gap for Claude is cross-session search, so this is
just that one tool, wired in-process (no subprocess/network hop — see
claude_agent_sdk.create_sdk_mcp_server, "better performance than external
MCP servers") rather than over stdio/http like the user-added integrations.

Skills tools added 2026-09-01 (David's ask: "all models... utilize and
operate under jarvis's methods, skills, and memory, all as a hive mind") —
a second real gap found the same way: Skills live in data/skills/, a
sibling of the vault directory, not inside it, so Claude's native file
tools (scoped to its vault cwd) genuinely can't see them either, same as
cross-session history before this file existed.

Notes/Tasks/Calendar/Specs tools added 2026-09-01, same day, after David
asked Claude about upcoming events/tasks and it said there weren't any —
same class of gap again: that's real app data under data/*.json, nowhere
near the vault cwd. Deliberately NOT raw file access to data/ itself (see
core/memory_tools.py's docstring — that directory also holds password
hashes, session tokens, and encrypted API keys); these go through the same
service layer the app's own routes use.

Documents/Contacts/task-run-history added 2026-09-01, same day, closing
the remaining gaps from an explicit item-by-item audit David asked for
("does the ai model know where to grab attachments, documents, sessions,
skills, vault, calendar_events.json, contacts.json...").
"""
from claude_agent_sdk import create_sdk_mcp_server, tool

from core import memory_tools


def get_hive_mind_server(exclude_session_id: str | None = None):
    """exclude_session_id isn't threaded into the tool call itself (the SDK
    tool signature is fixed at server-creation time) — Brain passes its own
    session id in by building a fresh server per connection instead of one
    shared global instance, so a session never "finds" its own history."""
    @tool(
        "search_sessions",
        "Search across every other chat session's message history for a keyword or phrase. "
        "Use this to recall something discussed in a different conversation. Returns short "
        "snippets, not full histories — call again with a more specific query to narrow results.",
        {"query": str},
    )
    async def _search(args: dict) -> dict:
        results = memory_tools.search_sessions(args["query"], exclude_session_id=exclude_session_id, max_results=5)
        if not results:
            text = "No matches in other sessions."
        else:
            text = "\n\n".join(
                f"[{r['session_title']}] ({r['role']}): {r['snippet']}" for r in results
            )
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_skills",
        "List every available Skill (a portable, saved procedure for how to do something) by "
        "name and one-line description. Skills live outside the vault, so this is the only way "
        "to discover them — call read_skill afterward for the full procedure.",
        {},
    )
    async def _list_skills(args: dict) -> dict:
        skills = memory_tools.list_skills()
        if not skills:
            text = "No skills saved yet."
        else:
            text = "\n".join(f"- {s['slug']}: {s['description'] or '(no description)'}" for s in skills)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "read_skill",
        "Read one Skill's full procedure by its slug (from list_skills).",
        {"slug": str},
    )
    async def _read_skill(args: dict) -> dict:
        try:
            text = memory_tools.read_skill(args["slug"])
        except ValueError as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_notes",
        "List open (not-yet-completed) Notes — todos, reminders, priorities. Use this for "
        "anything like \"what do I need to do\" or \"what's on my priorities list\".",
        {},
    )
    async def _list_notes(args: dict) -> dict:
        notes = memory_tools.list_notes()
        text = "\n".join(f"- {n['text']}" + (f" (due {n['due_date']})" if n.get("due_date") else "") for n in notes) or "No open notes."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_tasks",
        "List scheduled/automated Tasks (recurring or one-shot jobs JARVIS runs on its own) — "
        "distinct from Notes' todos.",
        {},
    )
    async def _list_tasks(args: dict) -> dict:
        tasks = memory_tools.list_tasks()
        text = "\n".join(f"- {t['name']} ({'enabled' if t.get('enabled') else 'disabled'})" for t in tasks) or "No tasks configured."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_upcoming_events",
        "List upcoming Calendar events and due-dated Notes for the next 14 days. Use this for "
        "\"what's coming up\" / \"do I have anything scheduled\" questions.",
        {},
    )
    async def _list_events(args: dict) -> dict:
        events = memory_tools.list_upcoming_events()
        text = "\n".join(f"- {e['title']} ({e['start']})" for e in events) or "Nothing upcoming in the next 14 days."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "create_note",
        "Create a new Note (a todo/reminder/priority item). Returns the created note.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "due_date": {"type": "string", "description": "Optional ISO 8601 datetime, e.g. 2026-09-01T15:00:00"},
                "project": {"type": "string", "description": "Defaults to 'personal' if omitted"},
            },
            "required": ["text"],
        },
    )
    async def _create_note(args: dict) -> dict:
        note = memory_tools.create_note(args["text"], due_date=args.get("due_date"), project=args.get("project", "personal"))
        return {"content": [{"type": "text", "text": f"Created note {note['id']}: {note['text']}"}]}

    @tool(
        "update_note",
        "Update an existing Note by id (from list_notes) — only pass the fields you want to change. "
        "Use completed=true to mark it done.",
        {
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
    )
    async def _update_note(args: dict) -> dict:
        note_id = args.pop("note_id")
        try:
            note = memory_tools.update_note(note_id, **{k: v for k, v in args.items() if v is not None})
            text = f"Updated note {note['id']}"
        except KeyError as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "delete_note",
        "Delete a Note by id (from list_notes). Irreversible.",
        {"note_id": str},
    )
    async def _delete_note(args: dict) -> dict:
        memory_tools.delete_note(args["note_id"])
        return {"content": [{"type": "text", "text": f"Deleted note {args['note_id']}"}]}

    @tool(
        "create_task",
        "Create a new scheduled/automated Task. schedule_kind is 'once' (needs run_at, an ISO "
        "datetime), 'interval' (needs interval_seconds), or 'daily' (needs run_time — use this "
        "whenever the user names a time of day, e.g. 'every morning at 6am').",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "prompt": {"type": "string", "description": "What the task should do when it runs"},
                "schedule_kind": {"type": "string", "enum": ["once", "interval", "daily"]},
                "run_at": {"type": "string", "description": "ISO 8601 datetime, required for schedule_kind='once'"},
                "interval_seconds": {"type": "integer", "description": "Required for schedule_kind='interval'"},
                "run_time": {"type": "string", "description": "Local time of day as 'HH:MM' (24-hour), required for schedule_kind='daily'"},
                "deliver_to_channel": {"type": "string", "description": "Optional comms channel key to post the result to"},
            },
            "required": ["name", "prompt", "schedule_kind"],
        },
    )
    async def _create_task(args: dict) -> dict:
        try:
            task = memory_tools.create_task(
                args["name"], args["prompt"], args["schedule_kind"],
                run_at=args.get("run_at"), interval_seconds=args.get("interval_seconds"),
                deliver_to_channel=args.get("deliver_to_channel"),
                run_time=args.get("run_time"),
            )
            text = f"Created task {task['id']}: {task['name']}"
        except ValueError as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "update_task",
        "Update an existing Task by id (from list_tasks) — only pass the fields you want to change. "
        "Use enabled=false to pause it.",
        {
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
    )
    async def _update_task(args: dict) -> dict:
        task_id = args.pop("task_id")
        try:
            task = memory_tools.update_task(task_id, **{k: v for k, v in args.items() if v is not None})
            text = f"Updated task {task['id']}"
        except KeyError as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "delete_task",
        "Delete a Task by id (from list_tasks). Irreversible.",
        {"task_id": str},
    )
    async def _delete_task(args: dict) -> dict:
        memory_tools.delete_task(args["task_id"])
        return {"content": [{"type": "text", "text": f"Deleted task {args['task_id']}"}]}

    @tool(
        "create_event",
        "Create a new Calendar event.",
        {
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
    )
    async def _create_event(args: dict) -> dict:
        event = memory_tools.create_event(
            args["title"], args["start"], args["end"],
            all_day=args.get("all_day", False), location=args.get("location", ""),
            description=args.get("description", ""),
        )
        return {"content": [{"type": "text", "text": f"Created event {event['id']}: {event['title']}"}]}

    @tool(
        "update_event",
        "Update an existing Calendar event by id (from list_upcoming_events) — only pass the fields "
        "you want to change. Use completed=true to check it off.",
        {
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
    )
    async def _update_event(args: dict) -> dict:
        event_id = args.pop("event_id")
        try:
            event = memory_tools.update_event(event_id, **{k: v for k, v in args.items() if v is not None})
            text = f"Updated event {event['id']}"
        except KeyError as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "delete_event",
        "Delete a Calendar event by id (from list_upcoming_events). Irreversible.",
        {"event_id": str},
    )
    async def _delete_event(args: dict) -> dict:
        memory_tools.delete_event(args["event_id"])
        return {"content": [{"type": "text", "text": f"Deleted event {args['event_id']}"}]}

    @tool(
        "list_specs",
        "List every architecture/subsystem spec doc (specs/*.md) — real documentation about how "
        "JARVIS itself is built (auth, frontend style, etc.), not user data.",
        {},
    )
    async def _list_specs(args: dict) -> dict:
        specs = memory_tools.list_specs()
        text = "\n".join(f"- {s}" for s in specs) or "No spec docs found."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "read_spec",
        "Read one spec doc in full by filename (from list_specs).",
        {"filename": str},
    )
    async def _read_spec(args: dict) -> dict:
        try:
            text = memory_tools.read_spec(args["filename"])
        except (ValueError, OSError) as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_documents",
        "List every document in the Library (title/tags only, not content) — call "
        "read_document afterward for the full text.",
        {},
    )
    async def _list_documents(args: dict) -> dict:
        docs = memory_tools.list_documents()
        text = "\n".join(f"- {d['id']}: {d['title']}" for d in docs) or "No documents in the Library."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "read_document",
        "Read one Library document's full content by id (from list_documents).",
        {"doc_id": str},
    )
    async def _read_document(args: dict) -> dict:
        try:
            text = memory_tools.read_document(args["doc_id"])
        except ValueError as e:
            text = str(e)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_contacts",
        "List synced contacts (name/email/phone).",
        {},
    )
    async def _list_contacts(args: dict) -> dict:
        contacts = memory_tools.list_contacts()
        text = "\n".join(f"- {c['name']}: {c.get('email') or ''} {c.get('phone') or ''}".strip() for c in contacts) or "No contacts synced."
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "list_task_runs",
        "List recent Task execution history — what a scheduled/automated Task actually produced "
        "when it last ran. Use this for \"what did that task find/do\" questions.",
        {},
    )
    async def _list_task_runs(args: dict) -> dict:
        runs = memory_tools.list_task_runs()
        text = "\n\n".join(f"[{r['task_name']}]: {r['output'] or r.get('error') or '(no output)'}" for r in runs) or "No task runs recorded yet."
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name="hive_mind", tools=[
        _search, _list_skills, _read_skill,
        _list_notes, _list_tasks, _list_events, _list_specs, _read_spec,
        _list_documents, _read_document, _list_contacts, _list_task_runs,
        _create_note, _update_note, _delete_note,
        _create_task, _update_task, _delete_task,
        _create_event, _update_event, _delete_event,
    ])
