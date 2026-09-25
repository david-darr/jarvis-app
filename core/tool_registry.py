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

from core import image_gen, memory_tools

CLAUDE, OPENAI = "claude", "openai"
BOTH = frozenset({CLAUDE, OPENAI})


@dataclass(frozen=True)
class ToolContext:
    session_id: Optional[str] = None
    is_admin: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]
    surfaces: frozenset = BOTH
    admin_only: bool = False


def _object(properties: Optional[dict] = None, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties or {}, "required": list(required)}


def _str(description: Optional[str] = None) -> dict:
    return {"type": "string", **({"description": description} if description else {})}


_REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, description: str, schema: dict, surfaces: frozenset = BOTH, admin_only: bool = False):
    def decorate(handler):
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} registered twice")
        _REGISTRY[name] = ToolSpec(name, description, schema, handler, surfaces, admin_only)
        return handler
    return decorate


def specs(surface: str, is_admin: bool = False) -> list[ToolSpec]:
    """The tools one surface offers, in registration order (stable, so the
    tool list - part of the cached prompt prefix - never reorders)."""
    return [s for s in _REGISTRY.values() if surface in s.surfaces and (is_admin or not s.admin_only)]


async def call(name: str, args: dict, ctx: ToolContext, surface: str) -> str:
    spec = _REGISTRY.get(name)
    if spec is None or surface not in spec.surfaces or (spec.admin_only and not ctx.is_admin):
        return f"Unknown tool: {name}"
    try:
        return await spec.handler(dict(args or {}), ctx)
    except Exception as e:
        return f"Tool error: {e}"


def openai_tools(is_admin: bool = False) -> list[dict]:
    """The OpenAI function-calling list for one session."""
    return [{"type": "function", "function": {"name": s.name, "description": s.description, "parameters": s.schema}}
            for s in specs(OPENAI, is_admin)]


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
        return memory_tools.format_archive_hits(memory_tools.search_this_chat_archive(ctx.session_id, args["query"]))
    results = memory_tools.search_sessions(args["query"], exclude_session_id=ctx.session_id, max_results=5)
    return "\n\n".join(f"[{r['session_title']}] ({r['role']}): {r['snippet']}" for r in results) \
        or "No matches in other sessions."


@register(
    "list_skills",
    "List every available Skill (a portable, saved procedure for how to do something) by "
    "name and one-line description. Skills live outside the vault, so this is the only way "
    "to discover them — call read_skill afterward for the full procedure.",
    _object(),
)
async def _list_skills(args, ctx):
    skills = memory_tools.list_skills()
    return "\n".join(f"- {s['slug']}: {s['description'] or '(no description)'}" for s in skills) or "No skills saved yet."


@register("read_skill", "Read one Skill's full procedure by its slug (from list_skills).",
          _object({"slug": _str("The skill's slug, from list_skills")}, ("slug",)))
async def _read_skill(args, ctx):
    return memory_tools.read_skill(args["slug"])


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
    return memory_tools.read_document(args["doc_id"])


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
    task = memory_tools.create_task(
        args["name"], args["prompt"], args["schedule_kind"],
        run_at=args.get("run_at"), interval_seconds=args.get("interval_seconds"),
        deliver_to_channel=args.get("deliver_to_channel"),
        run_time=args.get("run_time"), depends_on=args.get("depends_on"), status=args.get("status"),
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
    return "\n\n".join(f"[{r['path']}]: {r['snippet']}" for r in memory_tools.search_vault(args.get("query", ""))) \
        or "No matches in the vault."


@register(
    "read_vault_file",
    "Read one specific vault note in full, by its relative path (as returned by search_vault).",
    _object({"path": _str("Relative path within the vault, e.g. 'Active Priorities.md'")}, ("path",)),
    surfaces=frozenset({OPENAI}),
)
async def _read_vault_file(args, ctx):
    return memory_tools.read_vault_file(args.get("path", ""))


@register(
    "list_repo_directory",
    "List one level of jarvis-app's own source tree (not recursive — call again with a sub-path to descend). Omit path to list the top-level accessible directories.",
    _object({"path": _str()}),
    surfaces=frozenset({OPENAI}),
)
async def _list_repo_directory(args, ctx):
    return "\n".join(memory_tools.list_repo_directory(args.get("path", ""))) or "(empty)"


@register(
    "read_repo_file",
    "Read one file from jarvis-app's own source, by path relative to the repo root (e.g. 'core/brain.py').",
    _object({"path": _str()}, ("path",)),
    surfaces=frozenset({OPENAI}),
)
async def _read_repo_file(args, ctx):
    return memory_tools.read_repo_file(args["path"])


@register(
    "write_repo_file",
    "Create or overwrite one file in jarvis-app's own source with the given full content (full-file replacement, not a patch/diff). Creates parent directories if needed.",
    _object({"path": _str(), "content": _str()}, ("path", "content")),
    surfaces=frozenset({OPENAI}),
)
async def _write_repo_file(args, ctx):
    return memory_tools.write_repo_file(args["path"], args["content"])


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
        surface=f"chat:{ctx.session_id}" if ctx.session_id else "none", tool="run_shell",
        arguments={"command": command}, title="Run a command on this computer",
        description=command[:300], is_admin=ctx.is_admin)
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
        "copy_repo": {"type": "boolean", "description": "Admin chats only: start with a copy of the JARVIS code"},
        "timeout_seconds": {"type": "integer", "description": "Optional, default 120, at most 900"},
        "internet": {"type": "boolean", "description": "Optional: reach public websites (e.g. to install a package). "
                                                       "The person is asked first each time."},
    }, ("command",)),
)
async def _run_code(args, ctx):
    import json
    from core import sandbox
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
    try:
        timeout = int(args.get("timeout_seconds") or sandbox.DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = sandbox.DEFAULT_TIMEOUT_SECONDS
    internet = args.get("internet") in (True, "true", "True")
    if internet:
        # Code with the internet can upload what it reads, even through the
        # filter, so this one asks (David's call, 2026-09-24).
        from core import permissions
        decision = await permissions.decide(
            surface=f"chat:{ctx.session_id}" if ctx.session_id else "none", tool="run_code_internet",
            arguments={"command": command}, title="Run code with internet access",
            description=f"In the sandbox, reaching public websites only: {command[:300]}", is_admin=ctx.is_admin)
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
    if page["error"]:
        return f"Could not read {page['url']}: {page['error']}"
    links = "\n".join(f"- {l['text'] or '(no text)'}: {l['url']}" for l in page["links"]) or "(none)"
    return f"Title: {page['title']}\nURL: {page['url']}\n\n{page['text']}\n\nLinks:\n{links}"
