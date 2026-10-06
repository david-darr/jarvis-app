"""Lifecycle hooks (2026-10-05; spec: the vault note "Lifecycle Hooks (Build
Spec)"). A hook is a step of David's own that runs when something happens in
JARVIS: before or after a tool runs, a chat reply, a finished task run, a
card result, an agent report or question, a trigger event.

Four kinds: post signed JSON to a web address, add a line to a vault note,
send to a channel, or run a command on this computer. Only a command hook on
`tool.before` can block: exit code 2, or {"decision": "block", "reason"} on
stdout, stops the tool and the model is told why - in Auto and for agents
too. Everything else is notify-only and never slows the work down.

Adapted from Hermes Agent's agent/shell_hooks.py and agent/outbound_webhooks.py
(MIT License, Copyright (c) 2025 Nous Research): event JSON on stdin, exit 2
blocks, fail open unless told otherwise, a timeout that kills the process
tree, signed notify-only posts with two attempts. JARVIS changes: hooks are
created only by an admin in Settings (no model, tool or channel can see or
change them; creation is the consent), event data never reaches a command's
command line or environment, the JARVIS internal secrets are removed from a
command's environment, hooks never fire hooks, and one switch pauses all.
"""
import asyncio
import contextvars
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

logger = logging.getLogger(__name__)

HOOKS_FILE = os.path.join(DATA_DIR, "hooks.json")

EVENTS = {
    "tool.before": "Before a tool runs",
    "tool.after": "After a tool ran",
    "chat.reply": "A chat reply finished",
    "task.finished": "A task or agent goal run finished",
    "card.review": "A card's result is ready for review",
    "agent.inbox": "An agent sent a report or a question",
    "trigger.event": "A webhook trigger received an event",
}
TOOL_EVENTS = ("tool.before", "tool.after")
ACTIONS = ("webhook", "vault_note", "channel", "command")
TEXT_LIMIT = 4000
LOG_LENGTH = 50
WEBHOOK_TIMEOUT = 10
WEBHOOK_ATTEMPTS = 2
COMMAND_TIMEOUT = 30
COMMAND_MAX_TIMEOUT = 120
OUTPUT_LIMIT = 64_000
MAX_IN_FLIGHT = 100
BLOCK_EXIT_CODE = 2
# Never handed to a hook's command: they would let it call JARVIS as JARVIS.
PRIVATE_ENV = ("JARVIS_INTERNAL_TOKEN", "JARVIS_UI_SECRET", "JARVIS_SWARM_BRIDGE_TOKEN", "JARVIS_GOOGLE_CHAT_TOKEN",
               "JARVIS_CODEX_SESSION_ID", "JARVIS_SWARM_BRIDGE_PORT", "JARVIS_API_BASE")

# Set while a hook's own action runs, so nothing it causes fires hooks again.
_in_hook: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_in_hook", default=False)


def _clip(value):
    if isinstance(value, str):
        return value[:TEXT_LIMIT]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value[:50]]
    return value


def _summary(event: str, data: dict) -> str:
    if event in TOOL_EVENTS:
        return data.get("tool", "")
    for key in ("reply", "output", "text", "error", "outcome"):
        if data.get(key):
            return " ".join(str(data[key]).split())[:300]
    return ""


def render(template: str, payload: dict) -> str:
    from services.trigger_service import render as render_fields
    return render_fields(template, payload, payload.get("event", ""))


class HookService:
    def __init__(self) -> None:
        data = read_json(HOOKS_FILE, {})
        self._hooks: dict = data.get("hooks", {})
        self.paused: bool = bool(data.get("paused"))
        self._in_flight: set = set()

    def _save(self) -> None:
        write_json_atomic(HOOKS_FILE, {"hooks": self._hooks, "paused": self.paused})

    # -- the hooks -----------------------------------------------------------------

    def hooks(self) -> list[dict]:
        return sorted(self._hooks.values(), key=lambda h: h["created_at"])

    def get(self, hook_id: str) -> Optional[dict]:
        return self._hooks.get(hook_id)

    @staticmethod
    def public(hook: dict) -> dict:
        shown = {k: v for k, v in hook.items() if k != "config"}
        config = dict(hook.get("config") or {})
        if "secret" in config:
            config["secret"] = bool(config["secret"])
        shown["config"] = config
        return shown

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._save()

    @staticmethod
    def _check(fields: dict) -> dict:
        """Validated config for the hook's kind."""
        from services.agent_service import agent_service
        if fields.get("event") not in EVENTS:
            raise ValueError(f"event is one of {', '.join(EVENTS)}")
        action = fields.get("action")
        if action not in ACTIONS:
            raise ValueError(f"a hook does one of {', '.join(ACTIONS)}")
        if fields.get("agent_id") and agent_service.get(fields["agent_id"]) is None:
            raise ValueError("that agent no longer exists")
        if fields.get("source") not in (None, "", "any", "chat", "task", "agent"):
            raise ValueError("source is any, chat, task or agent")
        config = dict(fields.get("config") or {})
        if action == "webhook":
            url = str(config.get("url") or "").strip()
            if not url.startswith(("https://", "http://")):
                raise ValueError("the web address must start with https:// or http://")
            config["url"] = url
        elif action == "vault_note":
            path = str(config.get("path") or "").strip().replace("\\", "/")
            if not path or path.startswith("/") or ".." in path.split("/") or ":" in path:
                raise ValueError("pick a note inside the vault, such as Logs/Agent results.md")
            config["path"] = path if path.endswith(".md") else path + ".md"
            config["template"] = str(config.get("template") or "- {time} {event}: {summary}")
        elif action == "channel":
            if not str(config.get("channel_id") or "").strip():
                raise ValueError("pick a channel")
            config["template"] = str(config.get("template") or "{event}: {summary}")
        elif action == "command":
            command = str(config.get("command") or "").strip()
            if not command:
                raise ValueError("write the command to run")
            config["command"] = command
            try:
                timeout = int(config.get("timeout") or COMMAND_TIMEOUT)
            except (TypeError, ValueError):
                raise ValueError("the timeout is a number of seconds")
            config["timeout"] = max(1, min(COMMAND_MAX_TIMEOUT, timeout))
            config["block_on_failure"] = bool(config.get("block_on_failure"))
        return config

    def create(self, name: str, **fields) -> dict:
        name = " ".join((name or "").split())
        if not name:
            raise ValueError("give the hook a name")
        config = self._check(fields)
        if fields["action"] == "webhook" and config.get("secret"):
            config["secret"] = encrypt(str(config["secret"]))
        hook = {"id": uuid.uuid4().hex[:12], "name": name[:80], "enabled": True, "event": fields["event"],
                "action": fields["action"], "tool_pattern": (fields.get("tool_pattern") or "").strip(),
                "agent_id": fields.get("agent_id") or None, "source": fields.get("source") or "any",
                "config": config, "created_at": time.time(), "last_run_at": None, "log": []}
        self._hooks[hook["id"]] = hook
        self._save()
        return hook

    EDITABLE = ("name", "enabled", "event", "action", "tool_pattern", "agent_id", "source", "config")

    def update(self, hook_id: str, **fields) -> dict:
        hook = self._require(hook_id)
        unknown = set(fields) - set(self.EDITABLE)
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))}")
        if set(fields) - {"enabled", "name"}:
            merged = {**hook, **fields}
            config = self._check(merged)
            if merged["action"] == "webhook":
                given = (fields.get("config") or {}).get("secret")
                old = hook["config"].get("secret") if hook["action"] == "webhook" else None
                config["secret"] = encrypt(str(given)) if given else old
            hook.update({k: v for k, v in fields.items() if k != "config"})
            hook["config"] = config
        if "name" in fields:
            hook["name"] = " ".join((fields["name"] or "").split())[:80] or hook["name"]
        if "enabled" in fields:
            hook["enabled"] = bool(fields["enabled"])
        self._save()
        return hook

    def delete(self, hook_id: str) -> None:
        self._require(hook_id)
        del self._hooks[hook_id]
        self._save()

    def _require(self, hook_id: str) -> dict:
        hook = self._hooks.get(hook_id)
        if hook is None:
            raise KeyError(f"no such hook: {hook_id}")
        return hook

    def _log(self, hook: dict, event: str, outcome: str, detail: str = "") -> None:
        hook["last_run_at"] = time.time()
        hook["log"] = ([{"at": hook["last_run_at"], "event": event, "outcome": outcome, "detail": detail[:500]}]
                       + hook.get("log", []))[:LOG_LENGTH]
        if hook["id"] in self._hooks:
            self._save()

    # -- matching ------------------------------------------------------------------

    @staticmethod
    def _matches(hook: dict, event: str, data: dict) -> bool:
        if not hook["enabled"] or hook["event"] != event:
            return False
        if hook.get("agent_id") and data.get("agent_id") != hook["agent_id"]:
            return False
        source = hook.get("source") or "any"
        if source != "any" and data.get("source") != source:
            return False
        if event in TOOL_EVENTS and hook.get("tool_pattern"):
            patterns = [p.strip() for p in hook["tool_pattern"].replace(",", "|").split("|") if p.strip()]
            if not any(fnmatch.fnmatchcase(data.get("tool", ""), p) for p in patterns):
                return False
        return True

    def matching(self, event: str, data: dict) -> list[dict]:
        if self.paused or _in_hook.get():
            return []
        return [h for h in self.hooks() if self._matches(h, event, data)]

    @staticmethod
    def payload(event: str, data: dict) -> dict:
        return _clip({"event": event, "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "at": time.time(),
                      "summary": _summary(event, data), **data})

    # -- firing --------------------------------------------------------------------

    def emit(self, event: str, data: dict) -> None:
        """A notify-only event. Returns at once; each matching hook runs in
        the background and is logged. Safe from code with no running loop
        (a script): then nothing runs."""
        hooks = self.matching(event, data)
        if not hooks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("hooks: %s fired outside the event loop; %d hook(s) skipped", event, len(hooks))
            return
        payload = self.payload(event, data)
        for hook in hooks:
            if len(self._in_flight) >= MAX_IN_FLIGHT:
                self._log(hook, event, "skipped", "too many hooks running at once")
                continue
            task = loop.create_task(self._run(hook, payload, blocking=False))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def before_tool(self, tool: str, arguments, **context) -> Optional[str]:
        """The reason to block this tool call, or None. Command hooks on
        tool.before run in order and can block; other kinds are notified."""
        data = {"tool": tool, "input": arguments if isinstance(arguments, dict) else {"value": arguments}, **context}
        hooks = self.matching("tool.before", data)
        if not hooks:
            return None
        payload = self.payload("tool.before", data)
        for hook in hooks:
            if hook["action"] != "command":
                self._spawn(hook, payload)
                continue
            reason = await self._run(hook, payload, blocking=True)
            if reason:
                return reason
        return None

    def after_tool(self, tool: str, arguments, result, **context) -> None:
        self.emit("tool.after", {"tool": tool, "input": arguments if isinstance(arguments, dict) else {"value": arguments},
                                 "result": str(result)[:TEXT_LIMIT], **context})

    def _spawn(self, hook: dict, payload: dict) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._run(hook, payload, blocking=False))
        except RuntimeError:
            return
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def drain(self) -> None:
        """Wait for background hooks (tests, shutdown)."""
        while self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    async def _run(self, hook: dict, payload: dict, *, blocking: bool) -> Optional[str]:
        token = _in_hook.set(True)
        event = payload["event"]
        try:
            action = hook["action"]
            if action == "webhook":
                await self._post(hook, payload)
            elif action == "vault_note":
                self._append_note(hook, payload)
            elif action == "channel":
                from core.channels import registry
                delivered = await registry.send_to_channel(hook["config"]["channel_id"], render(hook["config"]["template"], payload))
                if not delivered:
                    raise RuntimeError("the channel did not take the message")
            elif action == "command":
                return await self._command(hook, payload, blocking)
            self._log(hook, event, "ok")
        except Exception as e:  # a hook never breaks the work it watches
            logger.warning("hook %s (%s) failed: %s", hook["name"], hook["id"], e)
            self._log(hook, event, "failed", str(e))
        finally:
            _in_hook.reset(token)
        return None

    async def _post(self, hook: dict, payload: dict) -> None:
        import httpx
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "X-JARVIS-Event": payload["event"],
                   "X-JARVIS-Delivery": uuid.uuid4().hex}
        secret = hook["config"].get("secret")
        if secret:
            headers["X-JARVIS-Signature"] = "sha256=" + hmac.new(decrypt(secret).encode(), body, hashlib.sha256).hexdigest()
        last = None
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            for attempt in range(WEBHOOK_ATTEMPTS):
                try:
                    response = await client.post(hook["config"]["url"], content=body, headers=headers)
                    if response.status_code < 400:
                        return
                    last = f"the address answered {response.status_code}"
                except httpx.HTTPError as e:
                    last = str(e) or type(e).__name__
                if attempt + 1 < WEBHOOK_ATTEMPTS:
                    await asyncio.sleep(1)
        raise RuntimeError(last or "not delivered")

    def _append_note(self, hook: dict, payload: dict) -> None:
        from core.vault import resolve_vault_dir
        vault = os.path.realpath(resolve_vault_dir())
        path = os.path.realpath(os.path.join(vault, hook["config"]["path"]))
        if os.path.commonpath([vault, path]) != vault:
            raise RuntimeError("that note is outside the vault")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = render(hook["config"]["template"], payload).rstrip("\n")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if f.tell() and not line.startswith("\n"):
                f.write("\n")
            f.write(line)

    async def _command(self, hook: dict, payload: dict, blocking: bool) -> Optional[str]:
        """Run the person's command with the event as JSON on stdin. The
        command line is exactly what he wrote; no event data reaches it or
        the environment."""
        config = hook["config"]
        env = {k: v for k, v in os.environ.items() if k not in PRIVATE_ENV}
        env["JARVIS_HOOK_EVENT"] = payload["event"]  # the event's name only, never its data
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            config["command"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=os.path.expanduser("~"), **kwargs)
        event = payload["event"]
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(json.dumps(payload, ensure_ascii=False).encode()), timeout=config["timeout"])
        except asyncio.TimeoutError:
            _kill_tree(process.pid)
            await process.wait()
            self._log(hook, event, "timeout", f"stopped after {config['timeout']}s")
            return self._failure(hook, f"the hook \"{hook['name']}\" timed out") if blocking else None
        except asyncio.CancelledError:
            # The work it watches was stopped (a chat's Stop); so is the
            # command, rather than running on with nobody waiting for it.
            _kill_tree(process.pid)
            self._log(hook, event, "skipped", "the turn was stopped while the command ran")
            raise
        out = stdout[:OUTPUT_LIMIT].decode("utf-8", errors="replace").strip()
        err = stderr[:OUTPUT_LIMIT].decode("utf-8", errors="replace").strip()
        decision = None
        if out.startswith("{"):
            try:
                decision = json.loads(out)
            except ValueError:
                decision = None
        wants_block = process.returncode == BLOCK_EXIT_CODE or (
            isinstance(decision, dict) and (decision.get("decision") or decision.get("action")) == "block")
        if wants_block and blocking:
            reason = (decision or {}).get("reason") if isinstance(decision, dict) else None
            reason = str(reason or err or f"blocked by the hook \"{hook['name']}\"")[:1000]
            self._log(hook, event, "blocked", reason)
            return reason
        if process.returncode not in (0, BLOCK_EXIT_CODE):
            self._log(hook, event, "failed", f"exit code {process.returncode}: {err[:300]}")
            return self._failure(hook, f"the hook \"{hook['name']}\" failed") if blocking else None
        self._log(hook, event, "ok", (out or err)[:300])
        return None

    @staticmethod
    def _failure(hook: dict, reason: str) -> Optional[str]:
        """A failing blocking hook lets the tool go ahead unless it was set
        to block on failure."""
        return reason if hook["config"].get("block_on_failure") else None

    # -- a test event --------------------------------------------------------------

    async def test(self, hook_id: str) -> dict:
        hook = self._require(hook_id)
        samples = {
            "tool.before": {"tool": "Bash", "input": {"command": "echo hello"}, "source": "chat", "session_id": "test"},
            "tool.after": {"tool": "Bash", "input": {"command": "echo hello"}, "result": "hello", "source": "chat"},
            "chat.reply": {"source": "chat", "session_id": "test", "title": "A test chat", "message": "Hello?", "reply": "Hello, this is a test."},
            "task.finished": {"source": "task", "task": "A test task", "output": "Done.", "error": None},
            "card.review": {"source": "task", "card": "A test card", "output": "The result."},
            "agent.inbox": {"source": "agent", "agent": "Test agent", "kind": "report", "title": "A test report", "text": "Nothing to see."},
            "trigger.event": {"source": "trigger", "trigger": "A test trigger", "outcome": "waiting", "detail": "A test event."},
        }
        payload = self.payload(hook["event"], {**samples[hook["event"]], "test": True})
        reason = await self._run(hook, payload, blocking=hook["event"] == "tool.before")
        latest = (hook.get("log") or [{}])[0]
        return {"outcome": latest.get("outcome"), "detail": latest.get("detail"), "blocked": reason}


def _kill_tree(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


hook_service = HookService()
