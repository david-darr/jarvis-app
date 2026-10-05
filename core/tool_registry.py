"""The hive-mind tools, declared once.

Every model reaches JARVIS's memory through the same tools: search past chats,
skills, notes, tasks and the work board, calendar, documents, contacts,
specs. They used to be written out twice by hand - once as Claude's in-process
MCP server (core/hive_mind_server.py) and once as the function list and a
66-branch if-chain for OpenAI-compatible models (core/external_brain.py) - so
every change had to be made in both and the two drifted. Modelled on Hermes
Agent's tools/registry.py: each tool is one ToolSpec (name, description, JSON
schema, handler, which surfaces get it), and both brains build their tool
lists and dispatch from this table.

Surfaces: "claude" is the Claude Code CLI, which has its own file tools, so
the vault/repo file tools and the shell are not offered there; "openai" is
every OpenAI-compatible endpoint, which has no file access of its own. Codex
reaches the same memory_tools functions through mcp_servers/hive_mind_cli.py.

A handler takes the tool's arguments and a ToolContext and returns the text
the model reads. Errors come back as text too: a tool failing must not end
the turn.
"""
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from core.turn_taint import TurnTaint

from core import image_gen, memory_tools

CLAUDE, OPENAI = "claude", "openai"
BOTH = frozenset({CLAUDE, OPENAI})


@dataclass(frozen=True)
class ToolContext:
    session_id: Optional[str] = None
    is_admin: bool = False
    allow_user_tab_source: bool = False
    turn_taint: TurnTaint | None = None
    # Set when the run or chat belongs to an agent (services/agent_service.py):
    # it gets the agent-only tools, and its permission requests go to that
    # agent's inbox rather than a prompt nobody is watching.
    agent_id: Optional[str] = None

    @property
    def permission_surface(self) -> str:
        """Who the broker asks (core/permissions.py)."""
        if self.agent_id and not self.session_id:
            return f"agent:{self.agent_id}"
        return f"chat:{self.session_id}" if self.session_id else "none"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]
    surfaces: frozenset = BOTH
    admin_only: bool = False
    agent_only: bool = False


def _object(properties: Optional[dict] = None, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required)}


def _str(description: Optional[str] = None) -> dict:
    return {"type": "string", **({"description": description} if description else {})}


_REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, description: str, schema: dict, surfaces: frozenset = BOTH, admin_only: bool = False,
             agent_only: bool = False):
    def decorate(handler):
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} registered twice")
        _REGISTRY[name] = ToolSpec(name, description, schema, handler, surfaces, admin_only, agent_only)
        return handler
    return decorate


def specs(surface: str, is_admin: bool = False, agent: bool = False) -> list[ToolSpec]:
    """The tools one surface offers, in registration order (stable, so the
    tool list - part of the cached prompt prefix - never reorders). Agent-only
    tools appear only for an agent's runs and chats."""
    return [s for s in _REGISTRY.values() if surface in s.surfaces and (is_admin or not s.admin_only)
            and (agent or not s.agent_only)]


async def call(name: str, args: dict, ctx: ToolContext, surface: str) -> str:
    spec = _REGISTRY.get(name)
    if (spec is None or surface not in spec.surfaces or (spec.admin_only and not ctx.is_admin)
            or (spec.agent_only and not ctx.agent_id)):
        return f"Unknown tool: {name}"
    try:
        return await spec.handler(dict(args or {}), ctx)
    except Exception as e:
        return f"Tool error: {e}"


def openai_tools(is_admin: bool = False, agent: bool = False) -> list[dict]:
    """The OpenAI function-calling list for one session."""
    return [{"type": "function", "function": {"name": s.name, "description": s.description, "parameters": s.schema}}
            for s in specs(OPENAI, is_admin, agent)]


def _fields(args: dict, key: str) -> tuple[str, dict]:
    ident = args.pop(key)
    return ident, {k: v for k, v in args.items() if v is not None}


# -- memory: past chats, skills, notes, tasks, calendar, documents ------------

@register(
    "search_sessions",
    "Search across every other chat session's message history for a keyword or phrase. "
    "Use this to recall something discussed in a different conversation. Returns short "
    "snippets, not full histories — call again with a more specific query to narrow results. "
    "With this_chat=true it instead searches the earlier part of THIS chat that was compacted "
    "into a summary; use that only when the summary lacks a detail you need.",
    _object({"query": _str("Keyword or phrase to search for"),
             "this_chat": {"type": "boolean", "description": "Search this chat's compacted earlier messages instead of other chats"}},
            ("query",)),
)
async def _search_sessions(args, ctx):
    if args.get("this_chat"):
        result = memory_tools.format_archive_hits(memory_tools.search_this_chat_archive(ctx.session_id, args["query"]))
        if ctx.turn_taint and result:
            ctx.turn_taint.mark("chat history")
        return result
    results = memory_tools.search_sessions(args["query"], exclude_session_id=ctx.session_id, max_results=5)
    result = "\n\n".join(f"[{r['session_title']}] ({r['role']}): {r['snippet']}" for r in results) \
        or "No matches in other sessions."
    if ctx.turn_taint and results:
        ctx.turn_taint.mark("chat history")
    return result


@register(
    "list_skills",
    "List every available Skill (a portable, saved procedure for how to do something) by "
    "name and one-line description. For a specific topic, search_skills is shorter. "
    "Call read_skill afterward for the full procedure.",
    _object(),
)
async def _list_skills(args, ctx):
    skills = memory_tools.list_skills()
    if skills and ctx.turn_taint:
        ctx.turn_taint.mark("skill descriptions")
    return "\n".join(f"- {s['slug']}: {s['description'] or '(no description)'}" for s in skills) or "No skills saved yet."


@register("read_skill", "Read one Skill's full procedure by its slug (from search_skills or list_skills).",
          _object({"slug": _str("The skill's slug, from search_skills or list_skills")}, ("slug",)))
async def _read_skill(args, ctx):
    result = memory_tools.read_skill(args["slug"])
    if ctx.turn_taint:
        ctx.turn_taint.mark("skill content")
    return result


@register("search_skills", "Search available Skills by name and short description. Returns matching slugs; "
          "call read_skill for the full procedure only when needed.",
          _object({"query": _str("Topic or task to find a saved Skill for")}, ("query",)))
async def _search_skills(args, ctx):
    skills = memory_tools.search_skills(args["query"])
    if skills and ctx.turn_taint:
        ctx.turn_taint.mark("skill descriptions")
    return "\n".join(f"- {s['slug']}: {s['description'] or '(no description)'}" for s in skills) or "No matching Skills."


@register(
    "list_notes",
    "List open (not-yet-completed) Notes — todos, reminders, priorities. Use this for "
    "anything like \"what do I need to do\" or \"what's on my priorities list\".",
    _object(),
)
async def _list_notes(args, ctx):
    notes = memory_tools.list_notes()
    return "\n".join(f"- {n['text']}" + (f" (due {n['due_date']})" if n.get("due_date") else "") for n in notes) \
        or "No open notes."


@register(
    "list_tasks",
    "List scheduled/automated Tasks (recurring or one-shot jobs JARVIS runs on its own) — "
    "distinct from Notes' todos.",
    _object(),
)
async def _list_tasks(args, ctx):
    return "\n".join(memory_tools.describe_task(t) for t in memory_tools.list_tasks()) or "No tasks configured."


@register(
    "list_upcoming_events",
    "List upcoming Calendar events and due-dated Notes for the next 14 days. Use this for "
    "\"what's coming up\" / \"do I have anything scheduled\" questions.",
    _object(),
)
async def _list_upcoming_events(args, ctx):
    events = memory_tools.list_upcoming_events()
    return "\n".join(f"- {e['title']} ({e['start']})" for e in events) or "Nothing upcoming in the next 14 days."


@register(
    "list_specs",
    "List every architecture/subsystem spec doc (specs/*.md) — real documentation about how "
    "JARVIS itself is built (auth, frontend style, etc.), not user data.",
    _object(),
)
async def _list_specs(args, ctx):
    return "\n".join(f"- {s}" for s in memory_tools.list_specs()) or "No spec docs found."


@register("read_spec", "Read one spec doc in full by filename (from list_specs).",
          _object({"filename": _str("The spec's filename, from list_specs")}, ("filename",)))
async def _read_spec(args, ctx):
    return memory_tools.read_spec(args["filename"])


@register(
    "list_documents",
    "List every document in the Library (title/tags only, not content) — call "
    "read_document afterward for the full text.",
    _object(),
)
async def _list_documents(args, ctx):
    return "\n".join(f"- {d['id']}: {d['title']}" for d in memory_tools.list_documents()) or "No documents in the Library."


@register("read_document", "Read one Library document's full content by id (from list_documents).",
          _object({"doc_id": _str("The document's id, from list_documents")}, ("doc_id",)))
async def _read_document(args, ctx):
    result = memory_tools.read_document(args["doc_id"])
    if ctx.turn_taint:
        ctx.turn_taint.mark("Library document")
    return result


@register("list_contacts", "List synced contacts (name/email/phone).", _object())
async def _list_contacts(args, ctx):
    return "\n".join(f"- {c['name']}: {c.get('email') or ''} {c.get('phone') or ''}".strip()
                     for c in memory_tools.list_contacts()) or "No contacts synced."


@register(
    "list_task_runs",
    "List recent Task execution history — what a scheduled/automated Task actually produced "
    "when it last ran. Use this for \"what did that task find/do\" questions.",
    _object(),
)
async def _list_task_runs(args, ctx):
    return "\n\n".join(f"[{r['task_name']}]: {r['output'] or r.get('error') or '(no output)'}"
                       for r in memory_tools.list_task_runs()) or "No task runs recorded yet."


# -- writes: notes, tasks and the work board, calendar ------------------------

@register(
    "create_note",
    "Create a new Note (a todo/reminder/priority item). Returns the created note.",
    _object({"text": _str(), "due_date": _str("Optional ISO 8601 datetime, e.g. 2026-09-01T15:00:00"),
             "project": _str("Defaults to 'personal' if omitted")}, ("text",)),
)
async def _create_note(args, ctx):
    note = memory_tools.create_note(args["text"], due_date=args.get("due_date"), project=args.get("project", "personal"))
    return f"Created note {note['id']}: {note['text']}"


@register(
    "update_note",
    "Update an existing Note by id (from list_notes) — only pass the fields you want to change. "
    "Use completed=true to mark it done.",
    _object({"note_id": _str(), "text": _str(), "due_date": _str(), "project": _str(),
             "completed": {"type": "boolean"}}, ("note_id",)),
)
async def _update_note(args, ctx):
    note_id, fields = _fields(args, "note_id")
    return f"Updated note {memory_tools.update_note(note_id, **fields)['id']}"


@register("delete_note", "Delete a Note by id (from list_notes). Irreversible.",
          _object({"note_id": _str()}, ("note_id",)))
async def _delete_note(args, ctx):
    memory_tools.delete_note(args["note_id"])
    return f"Deleted note {args['note_id']}"


@register(
    "create_task",
    "Create a new scheduled/automated Task. schedule_kind is 'once' (needs run_at, an ISO "
    "datetime), 'interval' (needs interval_seconds), or 'daily' (needs run_time — use this "
    "whenever the user names a time of day, e.g. 'every morning at 6am'), or 'card' for one-off "
    "work on the board: JARVIS runs a 'ready' card by itself and puts the result up for the "
    "user's review; depends_on makes it wait for other cards and receive their results.",
    _object({
        "name": _str(),
        "prompt": _str("What the task should do when it runs"),
        "schedule_kind": {"type": "string", "enum": ["once", "interval", "daily", "card"]},
        "status": {"type": "string", "enum": ["backlog", "ready"], "description": "Cards only: 'ready' to run it, 'backlog' (default) to hold it"},
        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Cards only: ids of cards it waits for"},
        "run_at": _str("ISO 8601 datetime, required for schedule_kind='once'"),
        "interval_seconds": {"type": "integer", "description": "Required for schedule_kind='interval'"},
        "run_time": _str("Local time of day as 'HH:MM' (24-hour), required for schedule_kind='daily'"),
        "deliver_to_channel": _str("Optional comms channel key to post the result to"),
    }, ("name", "prompt", "schedule_kind")),
)
async def _create_task(args, ctx):
    # Inside an agent's run or chat, new work is that agent's own.
    task = memory_tools.create_task(
        args["name"], args["prompt"], args["schedule_kind"],
        run_at=args.get("run_at"), interval_seconds=args.get("interval_seconds"),
        deliver_to_channel=args.get("deliver_to_channel"),
        run_time=args.get("run_time"), depends_on=args.get("depends_on"), status=args.get("status"),
        agent_id=ctx.agent_id,
    )
    return f"Created task {task['id']}: {task['name']}"


@register(
    "update_task",
    "Update an existing Task by id (from list_tasks) — only pass the fields you want to change. "
    "Use enabled=false to pause it.",
    _object({"task_id": _str(), "name": _str(), "prompt": _str(), "enabled": {"type": "boolean"},
             "deliver_to_channel": _str(),
             "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Cards only: ids of cards it waits for"}},
            ("task_id",)),
)
async def _update_task(args, ctx):
    task_id, fields = _fields(args, "task_id")
    return f"Updated task {memory_tools.update_task(task_id, **fields)['id']}"


@register("delete_task", "Delete a Task by id (from list_tasks). Irreversible.",
          _object({"task_id": _str()}, ("task_id",)))
async def _delete_task(args, ctx):
    memory_tools.delete_task(args["task_id"])
    return f"Deleted task {args['task_id']}"


@register(
    "create_event",
    "Create a new Calendar event.",
    _object({"title": _str(), "start": _str("ISO 8601 datetime"), "end": _str("ISO 8601 datetime"),
             "all_day": {"type": "boolean"}, "location": _str(), "description": _str()}, ("title", "start", "end")),
)
async def _create_event(args, ctx):
    event = memory_tools.create_event(args["title"], args["start"], args["end"], all_day=args.get("all_day", False),
                                      location=args.get("location", ""), description=args.get("description", ""))
    return f"Created event {event['id']}: {event['title']}"


@register(
    "update_event",
    "Update an existing Calendar event by id (from list_upcoming_events) — only pass the fields "
    "you want to change. Use completed=true to check it off.",
    _object({"event_id": _str(), "title": _str(), "start": _str(), "end": _str(), "all_day": {"type": "boolean"},
             "location": _str(), "description": _str(), "completed": {"type": "boolean"}}, ("event_id",)),
)
async def _update_event(args, ctx):
    event_id, fields = _fields(args, "event_id")
    return f"Updated event {memory_tools.update_event(event_id, **fields)['id']}"


@register("delete_event", "Delete a Calendar event by id (from list_upcoming_events). Irreversible.",
          _object({"event_id": _str()}, ("event_id",)))
async def _delete_event(args, ctx):
    memory_tools.delete_event(args["event_id"])
    return f"Deleted event {args['event_id']}"


# -- Claude only: bring generated images and files into the chat -------------

@register(
    "save_generated_image",
    "The FINAL step after generating an image/poster/design in Canva. When the user asks to "
    "create, draw, generate, design, or make an image/picture/poster/flyer of something: first "
    "use Canva's own design tools (create/generate a design of the appropriate design_type, then "
    "get-export-formats + export-design to get a real image file) to actually produce it — this "
    "tool does not generate anything itself. Once export-design returns a download URL, call this "
    "tool with that URL to bring the image into this conversation. The result comes back as a "
    "markdown image link — you MUST include that exact markdown verbatim in your reply, on its "
    "own line, so it actually renders. If Canva isn't available in this session (no tool access, "
    "or a real error from Canva itself), say so plainly — do not fall back to describing an image "
    "in words instead, and do not invent a URL.",
    _object({"url": _str("The real download URL returned by Canva's export-design tool"),
             "description": _str("Short description of the image, used as the alt text")}, ("url", "description")),
    surfaces=frozenset({CLAUDE}),
)
async def _save_generated_image(args, ctx):
    try:
        result = await image_gen.import_image_from_url(args["url"])
        _register_artifact(ctx, result["url"])
    except Exception as e:
        return f"Couldn't fetch that image: {e}"
    alt = args["description"][:80].replace("[", "").replace("]", "")
    return f"Image saved. Include this exact markdown in your reply so it renders: ![{alt}]({result['url']})"


@register(
    "save_generated_file",
    "Bring a locally-created file into this conversation as a real downloadable attachment. Use "
    "this after actually writing a file to disk with a Bash-run script — for example a poster, "
    "flyer, or presentation built with python-pptx, a Word doc with python-docx, a spreadsheet "
    "with openpyxl, or a PDF with reportlab. This tool does not create anything itself, only "
    "the write-then-call-this-tool pattern works, not calling this on a file that doesn't exist. "
    "The result comes back as a markdown link — you MUST include that exact markdown verbatim in "
    "your reply, on its own line, so the user gets a real download link (in Discord it is sent as "
    "a real file attachment automatically).",
    _object({"path": _str("Local filesystem path to the file that was just created"),
             "description": _str("Short description of the file, used as the link text")}, ("path", "description")),
    surfaces=frozenset({CLAUDE}),
)
async def _save_generated_file(args, ctx):
    try:
        result = image_gen.register_generated_file(args["path"])
        _register_artifact(ctx, result["url"])
    except Exception as e:
        return f"Couldn't save that file: {e}"
    desc = args["description"][:80].replace("[", "").replace("]", "")
    return f"File saved. Include this exact markdown link in your reply so it renders as a download: [{desc}]({result['url']})"


def _register_artifact(ctx: ToolContext, url: str) -> None:
    if ctx.session_id:
        from core.session_manager import session_manager
        session_manager.register_artifact(ctx.session_id, url)


# -- OpenAI-compatible only: the file access Claude has natively --------------

@register(
    "search_vault",
    "Search JARVIS's memory (the Obsidian vault) for notes matching a keyword or phrase. Returns short snippets, not full files.",
    _object({"query": _str("Keyword or phrase to search for")}, ("query",)),
    surfaces=frozenset({OPENAI}),
)
async def _search_vault(args, ctx):
    results = memory_tools.search_vault(args.get("query", ""))
    if ctx.turn_taint and results:
        ctx.turn_taint.mark("vault content")
    return "\n\n".join(f"[{r['path']}]: {r['snippet']}" for r in results) \
        or "No matches in the vault."


@register(
    "read_vault_file",
    "Read one specific vault note in full, by its relative path (as returned by search_vault).",
    _object({"path": _str("Relative path within the vault, e.g. 'Active Priorities.md'")}, ("path",)),
    surfaces=frozenset({OPENAI}),
)
async def _read_vault_file(args, ctx):
    result = memory_tools.read_vault_file(args.get("path", ""))
    if ctx.turn_taint:
        ctx.turn_taint.mark("vault file")
    return result


@register(
    "list_repo_directory",
    "List one level of jarvis-app source or user-built tab source (not recursive — call again with a sub-path to descend). Omit path to list the top-level accessible directories; custom-tabs/ is available only to supported OpenAI models.",
    _object({"path": _str()}),
    surfaces=frozenset({OPENAI}),
)
async def _list_repo_directory(args, ctx):
    path = args.get("path", "")
    normalized = path.replace("\\", "/").strip("/") if isinstance(path, str) else ""
    if normalized.startswith("custom-tabs") and not ctx.allow_user_tab_source:
        return "User-built tab source is not available to this model."
    entries = memory_tools.list_repo_directory(path)
    if not ctx.allow_user_tab_source and not normalized:
        entries = [entry for entry in entries if entry != "custom-tabs/"]
    return "\n".join(entries) or "(empty)"


@register(
    "read_repo_file",
    "Read one file from jarvis-app source (e.g. 'core/brain.py'); supported OpenAI models can also read user-built tab source (e.g. 'custom-tabs/routes/tab_minecraft.py').",
    _object({"path": _str()}, ("path",)),
    surfaces=frozenset({OPENAI}),
)
async def _read_repo_file(args, ctx):
    path = args["path"]
    if isinstance(path, str) and path.replace("\\", "/").lstrip("/").startswith("custom-tabs/") and not ctx.allow_user_tab_source:
        return "User-built tab source is not available to this model."
    result = memory_tools.read_repo_file(path)
    if ctx.turn_taint and isinstance(path, str) and path.replace("\\", "/").lstrip("/").startswith("custom-tabs/"):
        ctx.turn_taint.mark("user custom-tab source")
    return result


@register(
    "write_repo_file",
    "Create or overwrite one file in jarvis-app source with the given full content (full-file replacement, not a patch/diff). Supported OpenAI models can also write user tabs under custom-tabs/routes/, custom-tabs/services/, and custom-tabs/views/. Creates parent directories if needed.",
    _object({"path": _str(), "content": _str()}, ("path", "content")),
    surfaces=frozenset({OPENAI}),
)
async def _write_repo_file(args, ctx):
    path = args["path"]
    if isinstance(path, str) and path.replace("\\", "/").lstrip("/").startswith("custom-tabs/") and not ctx.allow_user_tab_source:
        return "User-built tab source is not available to this model."
    if isinstance(path, str) and path.replace("\\", "/").lstrip("/").startswith("custom-tabs/") and ctx.turn_taint and ctx.turn_taint.tainted:
        from core import permissions
        decision = await permissions.decide(
            surface=ctx.permission_surface, tool="write_custom_tab_source",
            arguments={"path": path}, title="Write custom-tab source after reading untrusted content",
            description=f"This turn read {ctx.turn_taint.reason}. Write {path}.", is_admin=ctx.is_admin,
            force_prompt=True)
        if decision.behavior != "allow":
            return f"Not run: {decision.reason or 'custom-tab source write was not approved'}"
    return memory_tools.write_repo_file(path, args["content"])


# Shell execution (David's ask 2026-09-02, modeled on Odysseus's own agent-tool
# gating): admin-only, and absent from a non-admin session's list entirely.
@register(
    "run_shell",
    "Run a shell command in jarvis-app's own repo root (or a given cwd). Use this to verify/run code you just wrote, e.g. a compile check or a test.",
    _object({"command": _str(), "cwd": _str("Optional, defaults to the jarvis-app repo root")}, ("command",)),
    surfaces=frozenset({OPENAI}),
    admin_only=True,
)
async def _run_shell(args, ctx):
    # Automatic through the seeded built-in grant (core/permissions.py); once
    # revoked in Settings > Permissions, each command is asked about instead.
    from core import permissions
    command = args.get("command", "")
    decision = await permissions.decide(
        surface=ctx.permission_surface, tool="run_shell",
        arguments={"command": command}, title="Run a command on this computer",
        description=(f"This turn read {ctx.turn_taint.reason}. " if ctx.turn_taint and ctx.turn_taint.tainted else "")
                    + command[:300], is_admin=ctx.is_admin,
        force_prompt=bool(ctx.turn_taint and ctx.turn_taint.tainted))
    if decision.behavior != "allow":
        return f"Not run: {decision.reason or 'not allowed'}"
    result = await memory_tools.run_shell(command, cwd=args.get("cwd"))
    return f"exit_code={result['exit_code']}\nstdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"


# Sandboxed code runs (Hermes track, phase 7 step 2, 2026-09-23). Every model
# gets it, with no permission prompt (David's call): a run happens in
# core/sandbox.py's container, with no network, no host files beyond a copy,
# no secrets, and nothing written back to a real folder - so there is nothing
# on this computer for a prompt to protect. Codex is not offered it; it
# already runs commands in its own sandbox.
@register(
    "run_code",
    "Run a shell command in an isolated Linux sandbox (Debian, Python 3.12; no internet unless internet=true, "
    "which asks the person first). "
    "Its working directory /work starts with the files you pass (path -> text) and, in an admin chat "
    "with copy_repo=true, a copy of the JARVIS code. Returns the output and a diff of what the command "
    "changed; nothing is written back to any real folder. Use it to run code, tests or scripts safely.",
    _object({
        "command": _str("A shell command run with sh in /work, not Python source. To run Python, put the "
                        "code in files (e.g. {\"main.py\": \"...\"}) and use the command \"python main.py\""),
        "files": {"type": "object", "additionalProperties": {"type": "string"},
                  "description": "Optional files to create first: relative path -> text content"},
        "copy_repo": {"type": "boolean", "description": "Admin chats only, and only when JARVIS runs from a "
                                                        "development checkout: start with a copy of the JARVIS code"},
        "timeout_seconds": {"type": "integer", "description": "Optional, default 120, at most 900"},
        "internet": {"type": "boolean", "description": "Optional: reach public websites (e.g. to install a package). "
                                                       "The person is asked first each time."},
    }, ("command",)),
)
async def _run_code(args, ctx):
    import json
    from core import sandbox
    from core.sandbox_changes import available as sandbox_changes_available
    from core.constants import BASE_DIR
    command = (args.get("command") or "").strip()
    if not command:
        return "Not run: no command given."
    files = args.get("files") or {}
    if isinstance(files, str):  # some local models send objects as JSON text
        try:
            files = json.loads(files)
        except ValueError:
            return "Not run: files must be an object of path -> text."
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        return "Not run: files must be an object of path -> text."
    copy_repo = args.get("copy_repo") in (True, "true", "True")
    if copy_repo and not ctx.is_admin:
        return "Not run: only an admin chat can copy the JARVIS code into the sandbox."
    if copy_repo and not sandbox_changes_available():
        return ("Not run: copy_repo only works when JARVIS runs from a development checkout of its code. "
                "This is an installed app, whose folder holds the bundled runtime and is replaced on update.")
    try:
        timeout = int(args.get("timeout_seconds") or sandbox.DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = sandbox.DEFAULT_TIMEOUT_SECONDS
    internet = args.get("internet") in (True, "true", "True")
    if internet:
        # Checked before asking: approving a run that then cannot happen
        # would be a prompt for nothing.
        ready, why = await sandbox.available()
        if not ready:
            return f"Not run: the sandbox is unavailable ({why}). Nothing was run on this computer."
        # Code with the internet can upload what it reads, even through the
        # filter, so this one asks (David's call, 2026-09-24).
        from core import permissions
        decision = await permissions.decide(
            surface=ctx.permission_surface, tool="run_code_internet",
            arguments={"command": command}, title="Run code with internet access",
            description=(f"This turn read {ctx.turn_taint.reason}. " if ctx.turn_taint and ctx.turn_taint.tainted else "")
                        + f"In the sandbox, reaching public websites only: {command[:300]}",
            is_admin=ctx.is_admin, force_prompt=bool(ctx.turn_taint and ctx.turn_taint.tainted))
        if decision.behavior != "allow":
            return f"Not run: {decision.reason or 'internet access was not allowed'}"
    try:
        result = await sandbox.run(command, files=files, source_dir=BASE_DIR if copy_repo else None,
                                   timeout=timeout, network=internet)
    except sandbox.SandboxUnavailable as e:
        return f"Not run: the sandbox is unavailable ({e}). Nothing was run on this computer."
    except ValueError as e:
        return f"Not run: {e}"
    changes = "\n".join(f"  {c['status']}: {c['path']}" for c in result.changes) or "  none"
    kept = ""
    if copy_repo:
        # Edits to the JARVIS code wait for the person (core/sandbox_changes.py);
        # there is deliberately no tool that applies them.
        from core import sandbox_changes
        change_id = sandbox_changes.record(result, ctx.session_id)
        if change_id:
            kept = (f"These edits were kept as change set {change_id}. They are NOT applied: the person reviews "
                    "and applies them in Settings > Administration > Sandbox changes. Tell them it is waiting.\n")
    return (f"exit_code={result.exit_code}{' (timed out)' if result.timed_out else ''}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\n"
            f"files changed in /work (not written anywhere real):\n{changes}\n" + kept
            + (f"diff:\n{result.diff}" if result.diff else ""))


# The read-only browser (Hermes phase 7 step 3, 2026-09-24): a fresh headless
# Chromium in the sandbox, public websites only, nothing kept between calls
# (core/sandbox_browser.py). No permission prompt (David's call): it reads
# public pages, much like a search.
@register(
    "browse",
    "Open one public web page in a sandboxed browser and read it: returns the page title, its readable text "
    "(first 12,000 characters) and its links. Public websites only; no logins or cookies. Use it to read a page "
    "you have the address of, e.g. documentation or an article.",
    _object({"url": _str("The full http:// or https:// address")}, ("url",)),
)
async def _browse(args, ctx):
    from core import sandbox, sandbox_browser
    try:
        page = await sandbox_browser.browse(args.get("url") or "")
    except ValueError as e:
        return f"Not opened: {e}"
    except sandbox.SandboxUnavailable as e:
        return f"Not opened: the sandbox is unavailable ({e})."
    if ctx.turn_taint:
        ctx.turn_taint.mark("browse result")
    if page["error"]:
        return f"Could not read {page['url']}: {page['error']}"
    links = "\n".join(f"- {l['text'] or '(no text)'}: {l['url']}" for l in page["links"]) or "(none)"
    return f"Title: {page['title']}\nURL: {page['url']}\n\n{page['text']}\n\nLinks:\n{links}"


# Google Workspace uses one permission decision per mutation, inside the
# shared handler. The CLI wrapper calls that same handler through the backend.
_google_fields = {
    "action": _str("Operation to perform"), "file_id": _str("Google file ID"),
    "query": _str("Drive name search"), "parent": _str("Drive folder ID"),
    "kind": _str("all, folder, sheet, or form"), "trashed": {"type": "boolean"},
    "page_token": _str(), "name": _str(), "permission_id": _str(),
    "email": _str(), "role": _str("reader, commenter, or writer"),
    "permission_type": _str("user, group, domain, or anyone"), "domain": _str(),
    "title": _str(), "cell_range": _str("Sheets A1 range"),
    "local_path": _str("Local file to upload to Google Drive, up to 25 MB"),
    "values": {"type": "array"}, "requests": {"type": "array"},
    "published": {"type": "boolean"},
}


@register("google_drive", "Manage connected Google Drive. Actions: list, info, upload, create_folder, rename, move, copy, trash, restore, star, unstar, delete, permissions, share, update_permission, revoke, revisions. Use Library to connect an account first.",
          _object(_google_fields, ("action",)), admin_only=True)
async def _google_drive(args, ctx):
    from core.google_chat_tools import execute
    return await execute("drive", args, ctx)


@register("google_sheets", "Read and edit Google Sheets. Actions: get (metadata), values (provide cell_range), create, update, append, clear, batch. Use Google Drive list to find spreadsheet IDs.",
          _object(_google_fields, ("action",)), admin_only=True)
async def _google_sheets(args, ctx):
    from core.google_chat_tools import execute
    return await execute("sheets", args, ctx)


@register("google_forms", "Create and manage Google Forms. Actions: get, responses, create, batch, publish. Use Google Drive list to find form IDs.",
          _object(_google_fields, ("action",)), admin_only=True)
async def _google_forms(args, ctx):
    from core.google_chat_tools import execute
    return await execute("forms", args, ctx)


# -- agents: only in an agent's own runs and chats (services/agent_service.py) --

@register(
    "agent_remember",
    "Write a note to your own memory so later runs have it. section is one of 'About this work', "
    "'Preferences', 'Corrections', 'Notes'. Keep each note to one short line.",
    _object({"section": _str("About this work, Preferences, Corrections or Notes"),
             "text": _str("The note, one line")}, ("section", "text")),
    agent_only=True,
)
async def _agent_remember(args, ctx):
    from services.agent_service import agent_service
    agent_service.remember(ctx.agent_id, args["section"], args["text"])
    return f"Noted under {args['section']}."


@register(
    "agent_ask",
    "Ask the person a question that needs their decision before you can continue. It goes to "
    "their inbox; their answer comes with your next run. After asking, finish what you can and stop.",
    _object({"question": _str("The question, with the options if there are any"),
             "context": _str("Optional: what you found that makes this a question")}, ("question",)),
    agent_only=True,
)
async def _agent_ask(args, ctx):
    from services.agent_service import agent_service
    item = agent_service.add_item(ctx.agent_id, "question", args["question"], args.get("context") or "")
    agent_service.notify(item)
    return "Your question is in the person's inbox. Finish what you can and stop; the answer comes with your next run."
