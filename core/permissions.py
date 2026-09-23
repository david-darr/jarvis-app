"""App-wide approval: ask before doing, once or always, and remember.

David's ask 2026-09-18. Until now a tool that was not pre-approved in code
simply hung on a prompt nothing could answer, or failed with a message saying
permission was missing. This is the missing half: a request that reaches the
person, inside whatever feature is asking.

Three rules shape everything here.

Fail closed. An unanswered request denies, and says it timed out rather than
implying the owner refused. Silence, a closed tab, or a surface with nobody
watching are all denials.

A person's "always" grant is scoped to a command prefix, path root or host.
The built-in admin Bash and PowerShell grants are the deliberate exception:
David restored automatic admin shell commands on 2026-09-22. They remain visible
and revocable in Settings.

A grant you cannot find is a trap. Rules are listed and revocable in
Settings, and every decision is written to an audit trail.
"""
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field

from core.constants import DATA_DIR
from core.atomic_io import write_json_atomic

PERMISSIONS_FILE = os.path.join(DATA_DIR, "permissions.json")
DEFAULT_TIMEOUT_SECONDS = 120
AUDIT_LIMIT = 500

_lock = asyncio.Lock()
_channels: dict[str, asyncio.Queue] = {}
_pending: dict[str, "Request"] = {}
_session_rules: dict[str, list[dict]] = {}


@dataclass
class Request:
    id: str
    surface: str
    tool: str
    target: str | None
    arguments: dict
    title: str
    description: str
    choices: list[dict]
    answer: asyncio.Future = field(default_factory=asyncio.Future)

    def payload(self) -> dict:
        """What the surface shows. Text only - never markup, never executed."""
        return {"id": self.id, "tool": self.tool, "target": self.target,
                "title": self.title, "description": self.description,
                "arguments": _clip(self.arguments), "choices": self.choices}


@dataclass(frozen=True)
class Decision:
    behavior: str            # "allow" or "deny"
    reason: str = ""
    rule: dict | None = None


def _clip(value, limit=4000):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _load() -> dict:
    try:
        with open(PERMISSIONS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data.setdefault("rules", [])
    data.setdefault("audit", [])
    data.setdefault("seeded", [])
    return data


def _save(data: dict) -> None:
    data["audit"] = data["audit"][-AUDIT_LIMIT:]
    write_json_atomic(PERMISSIONS_FILE, data)


def _matches(rule: dict, tool: str, target: str | None, is_admin: bool = False) -> bool:
    """A rule with no content covers the whole tool, which is only ever used
    for the tools this app pre-approves for itself, including admin shell.
    A person's prompt-based grant carries content; :* is a prefix."""
    if rule["tool"] != tool:
        return False
    if rule.get("admin_only") and not is_admin:
        return False
    content = rule.get("content")
    if not content:
        return True
    if target is None:
        return False
    if content.endswith(":*"):
        return target.startswith(content[:-2])
    return target == content


def derive_target(tool: str, arguments: dict) -> str | None:
    """The part of a request a grant should be scoped to.

    Deliberately coarse on the left and specific on the right: a shell grant
    covers a command, not the shell; a file grant covers a directory, not the
    disk. When nothing sensible can be derived the request stays unscoped,
    which means it can only ever be allowed once.
    """
    if not isinstance(arguments, dict):
        return None
    for key in ("command", "cmd"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.strip().split()[:2])
    for key in ("path", "file_path", "filename", "doc_id", "slug"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = value.replace("\\", "/").strip("/")
            return cleaned.rsplit("/", 1)[0] or cleaned
    for key in ("url", "base_url", "to"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.split("/")[2] if "://" in value else value.strip()
    return None


def list_rules() -> list[dict]:
    data = _load()
    session = [rule for rules in _session_rules.values() for rule in rules]
    return data["rules"] + session


def audit() -> list[dict]:
    return _load()["audit"]


def revoke(rule_id: str) -> bool:
    data = _load()
    remaining = [rule for rule in data["rules"] if rule["id"] != rule_id]
    removed = len(remaining) != len(data["rules"])
    if removed:
        data["rules"] = remaining
        _record(data, {"decision": "revoked", "rule_id": rule_id})
        _save(data)
        return True
    for surface, rules in _session_rules.items():
        kept = [rule for rule in rules if rule["id"] != rule_id]
        if len(kept) != len(rules):
            _session_rules[surface] = kept
            return True
    return False


def ensure_seeded(tools: list[str]) -> None:
    """Make the app's own pre-approvals visible.

    These tools were already granted - hardcoded in core/brain.py, invisible
    and unrevocable. Writing them in as rules changes no behaviour and makes
    them inspectable and revocable. Admin shell tools are seeded when the
    admin config includes them; non-admin configs never do.
    """
    data = _load()
    known = set(data["seeded"])
    additions = [tool for tool in tools if tool not in known and tool != "run_shell"]
    if not additions:
        return
    now = time.time()
    for tool in additions:
        data["rules"].append({"id": uuid.uuid4().hex, "tool": tool, "content": None,
                              "behavior": "allow", "scope": "forever", "granted_at": now,
                              "granted_by": "jarvis", "source": "built-in",
                              "admin_only": tool in ("Bash", "PowerShell")})
        data["seeded"].append(tool)
    _save(data)


def standing_grants() -> set[str]:
    """Tools a saved rule allows whole and for good - what core/brain.py may
    still pre-approve. A seeded built-in grant that has been revoked is no
    longer here, so the tool goes back to being asked about."""
    return {rule["tool"] for rule in _load()["rules"]
            if rule.get("behavior") == "allow" and not rule.get("content") and rule.get("scope") == "forever"}


def grant_standing(tools: list[str], granted_by: str) -> None:
    """Grant tools whole and for good, when someone explicitly adds them
    (Settings > Agent Tools' extra allowed list). Seeding happens once per
    tool, so without this a tool revoked earlier could never be re-allowed
    by adding it back. run_shell is never granted this way."""
    data = _load()
    held = {rule["tool"] for rule in data["rules"]
            if rule.get("behavior") == "allow" and not rule.get("content") and rule.get("scope") == "forever"}
    additions = [tool for tool in tools if tool not in held and tool != "run_shell"]
    if not additions:
        return
    now = time.time()
    for tool in additions:
        data["rules"].append({"id": uuid.uuid4().hex, "tool": tool, "content": None,
                              "behavior": "allow", "scope": "forever", "granted_at": now,
                              "granted_by": granted_by, "source": "settings",
                              "admin_only": tool in ("Bash", "PowerShell")})
        if tool not in data["seeded"]:
            data["seeded"].append(tool)
        _record(data, {"decision": "granted", "tool": tool, "by": granted_by})
    _save(data)


def _record(data: dict, entry: dict) -> None:
    data["audit"].append({"at": time.time(), **entry})


def _remember(rule: dict, surface: str) -> None:
    if rule["scope"] == "session":
        _session_rules.setdefault(surface, []).append(rule)
        return
    data = _load()
    data["rules"].append(rule)
    _record(data, {"decision": "granted", "tool": rule["tool"], "content": rule.get("content"),
                   "scope": rule["scope"], "by": rule.get("granted_by")})
    _save(data)


def stored_decision(surface: str, tool: str, target: str | None, is_admin: bool = False) -> Decision | None:
    """An answer that needs no one, or None if this has to be asked."""
    for rule in _session_rules.get(surface, []) + _load()["rules"]:
        if _matches(rule, tool, target, is_admin):
            return Decision(rule["behavior"], "A saved rule covers this.", rule)
    return None


def open_channel(surface: str) -> asyncio.Queue:
    """A surface that can ask. Closing it makes every later request deny."""
    queue: asyncio.Queue = asyncio.Queue()
    _channels[surface] = queue
    return queue


def close_channel(surface: str) -> None:
    _channels.pop(surface, None)
    for request in [item for item in _pending.values() if item.surface == surface]:
        if not request.answer.done():
            request.answer.set_result(Decision("deny", "The window asking for this closed."))


def drop_session(surface: str) -> None:
    _session_rules.pop(surface, None)


async def decide(*, surface: str, tool: str, arguments: dict, title: str = "", description: str = "",
                 target: str | None = None, choices: list[dict] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, is_admin: bool = False) -> Decision:
    """Answer from a rule, or ask the person and wait."""
    target = target if target is not None else derive_target(tool, arguments)
    saved = stored_decision(surface, tool, target, is_admin)
    if saved:
        return saved
    queue = _channels.get(surface)
    if queue is None:
        # Nobody is watching this surface - a scheduled task, a chat in a
        # channel with no prompt, a closed window. Denying is the only honest
        # answer, and it says where to grant it instead.
        return Decision("deny", f"{tool} needs permission and this surface cannot ask. "
                                "Grant it in Settings > Permissions, then try again.")
    request = Request(id=uuid.uuid4().hex, surface=surface, tool=tool, target=target,
                      arguments=arguments if isinstance(arguments, dict) else {},
                      title=title or tool, description=description,
                      choices=choices or _default_choices(tool, target))
    _pending[request.id] = request
    await queue.put(request.payload())
    try:
        return await asyncio.wait_for(asyncio.shield(request.answer), timeout=timeout)
    except asyncio.TimeoutError:
        return Decision("deny", f"Nobody answered the request to use {tool} within "
                                f"{int(timeout)} seconds, so it was not allowed.")
    finally:
        _pending.pop(request.id, None)


def _default_choices(tool: str, target: str | None) -> list[dict]:
    """What the person is offered. Always-grants state their own scope in
    words, so nobody clicks "always" without seeing what it covers."""
    choices = [{"id": "once", "label": "Allow once", "behavior": "allow", "scope": "once"}]
    if target:
        choices.append({"id": "session", "label": f"Always in this chat ({target})",
                        "behavior": "allow", "scope": "session"})
        choices.append({"id": "forever", "label": f"Always allow {target}",
                        "behavior": "allow", "scope": "forever"})
    choices.append({"id": "reject", "label": "Reject", "behavior": "deny", "scope": "once"})
    return choices


def answer(request_id: str, choice_id: str, user: str) -> bool:
    """Resolve a pending request. Request ids are server-made, so a model
    cannot answer its own question by guessing one."""
    request = _pending.get(request_id)
    if request is None or request.answer.done():
        return False
    choice = next((item for item in request.choices if item["id"] == choice_id), None)
    if choice is None:
        return False
    rule = None
    if choice["scope"] in ("session", "forever"):
        rule = {"id": uuid.uuid4().hex, "tool": request.tool, "content": request.target,
                "behavior": choice["behavior"], "scope": choice["scope"], "granted_at": time.time(),
                "granted_by": user, "source": "asked"}
        _remember(rule, request.surface)
    data = _load()
    _record(data, {"decision": choice["behavior"], "tool": request.tool, "content": request.target,
                   "scope": choice["scope"], "by": user, "request_id": request_id})
    _save(data)
    reason = ("Allowed by the owner." if choice["behavior"] == "allow"
              else f"{request.tool} was refused by the owner.")
    request.answer.set_result(Decision(choice["behavior"], reason, rule))
    return True
