"""Contained browsers keep web pages away from the person's desktop.

The host and Docker driver exchange numbered JSON lines on stdin/stdout. This
module enforces safety, never the model: new sites ask; buying, sending and
posting stop for the person; deleting asks; private fields and opaque frames
need the person. At most two computers run, and idle ones close after ten
minutes. Profiles start blank and are kept only for opted-in agents. See the
vault note "Computer Use (Build Spec)".
"""
import asyncio
import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from core import computer_image, permissions, sandbox, sandbox_browser, sandbox_egress
from core.computer_driver import SOURCE
from core.constants import DATA_DIR

logger = logging.getLogger(__name__)
IDLE_SECONDS = 600
MAX_RUNNING = 2
REPLY_LIMIT = 32 * 1024 * 1024
# Live-view frames a second (David, 2026-10-07): smooth while the person has
# control on this computer, lighter while they only watch, and never more
# than WATCH_FPS for a Remote Access viewer (about 8 Mbit/s at 10).
WATCH_FPS, CONTROL_FPS = 10, 30
RECORDING_LIMIT = 200
PROFILES = Path(DATA_DIR) / "computer" / "profiles"
# These commitments belong to the person even when an agent or chat runs in Auto.
PERSON_ACTION_WORDS = re.compile(
    r"\b(?:buy|pay|purchase|order|place\s+order|checkout|check\s+out|subscribe|donate|transfer|"
    r"send|reply|forward|post|repost|retweet|publish|share|comment|submit|confirm)\b", re.I)
# Public reactions on the person's account (David, 2026-10-07): theirs too by
# default, but an admin may let the computer press them
# (computer_use.allow_reactions). Buying, sending and posting never relax.
REACTION_WORDS = re.compile(
    r"\b(?:like|unlike|follow|unfollow|upvote|downvote|react|favou?rite|star|heart)\b", re.I)
DELETE_WORDS = re.compile(r"\b(?:delete|remove)\b", re.I)
PRIVATE_FIELDS = {"current-password", "new-password", "one-time-code"}
# The driver's source arrives as the first stdin line, not on the command
# line: Windows caps a command line at 32,767 characters, and the driver
# passed that in phase 3 ("The filename or extension is too long").
BOOTSTRAP = ("import json, sys; exec(compile(json.loads(sys.stdin.readline())['driver'], "
             "'<kairos-computer-driver>', 'exec'), {'__name__': '__main__'})")
DELETE_CHOICES = [{"id": "once", "label": "Allow once", "behavior": "allow", "scope": "once"},
                  {"id": "reject", "label": "Reject", "behavior": "deny", "scope": "once"}]


@dataclass
class Computer:
    owner: str
    proc: object
    name: str
    desktop: bool = False
    url: str = "about:blank"
    title: str = ""
    last_action: float = field(default_factory=time.monotonic)
    last_action_at: float = field(default_factory=time.time)
    screenshot: bytes | None = None
    frame_jpeg: bytes | None = None
    hosts: set[str] = field(default_factory=set)
    sequence: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    snapshot_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stderr_task: asyncio.Task | None = None
    taken_over: bool = False
    waiting_model: bool = False
    handback: asyncio.Event = field(default_factory=asyncio.Event)
    last_snapshot: float = -1.0
    # The driver's stdout carries replies and screencast frames; one reader
    # sorts them (_read), so the live view never waits behind a command.
    pending: dict = field(default_factory=dict)
    reader_task: asyncio.Task | None = None
    frame_seq: int = 0
    frame_changed: asyncio.Event = field(default_factory=asyncio.Event)
    watchers: int = 0
    local_watchers: int = 0
    cast_fps: int = 0
    recording: bool = False
    recording_id: str | None = None
    steps: list[dict] = field(default_factory=list)


class ComputerManager:
    def __init__(self, process_factory=None, clock=None):
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.clock = clock or time.monotonic
        self._computers: dict[str, Computer] = {}
        self._closed: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None

    def running(self) -> list[dict]:
        return [{"owner": c.owner, "url": c.url, "title": c.title, "last_action": c.last_action_at,
                 "taken_over": c.taken_over, "waiting_model": c.waiting_model, "desktop": c.desktop,
                 "recording": c.recording}
                for c in self._computers.values()]

    def screenshot(self, owner: str) -> bytes | None:
        c = self._computers.get(owner)
        return c.screenshot if c else None

    def is_running(self, owner: str) -> bool:
        return owner in self._computers

    def forget(self, owner: str) -> None:
        self._closed.pop(owner, None)

    def last(self, owner: str) -> dict | None:
        self._expire_frames()
        saved = self._closed.get(owner)
        return dict(saved[1]) if saved else None

    def _expire_frames(self):
        for owner, (deadline, _) in list(self._closed.items()):
            if self.clock() >= deadline:
                self._closed.pop(owner, None)

    async def _drain_stderr(self, proc):
        while line := await proc.stderr.readline():
            logger.debug("computer driver: %s", line.decode(errors="replace").rstrip())

    async def _start(self, owner: str, is_admin: bool = True) -> Computer:
        desktop = is_admin and _desktop_enabled()
        ok, why = await sandbox.available()
        if not ok:
            raise sandbox.SandboxUnavailable(why)
        try:
            await sandbox_egress.ensure(sandbox_browser.BROWSER_IMAGE)
        except RuntimeError as e:
            raise sandbox.SandboxUnavailable(f"no filtered network: {e}") from e
        image = await computer_image.ensure_image(desktop=desktop)
        profile_args = ["--tmpfs", "/profile:rw,nosuid,nodev,size=256m"]
        if owner.startswith("agent:"):
            from services.agent_service import agent_service
            agent_id = owner.removeprefix("agent:")
            agent = agent_service.get(agent_id)
            if agent and agent.get("keep_signed_in"):
                if not re.fullmatch(r"[0-9a-f]{10}", agent_id):
                    raise ValueError("bad agent id")
                root = PROFILES.resolve()
                if Path(DATA_DIR).resolve() not in root.parents:
                    raise ValueError("profile path is outside Kairos data")
                profile = PROFILES / agent_id
                profile.mkdir(parents=True, exist_ok=True)
                target = profile.resolve()
                if root not in target.parents:
                    raise ValueError("profile path is outside Kairos data")
                profile_args = ["--mount", f"type=bind,src={target},dst=/profile"]
        name = "jarvis-computer-" + uuid.uuid4().hex[:12]
        args = ["docker", "run", "-i", "--rm", "--name", name, "--label", sandbox.LABEL,
                *sandbox.hardening_args("2g" if desktop else "1g", "1", 512, network=True), *profile_args,
                "-e", "HOME=/profile", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", f"KAIROS_COMPUTER_PROXY={sandbox_egress.PROXY_URL}",
                "-e", f"KAIROS_COMPUTER_DESKTOP={int(desktop)}",
                image, "python3", "-u", "-c", BOOTSTRAP]
        try:
            # A reply carries a whole screenshot on one line, far past asyncio's
            # 64 KiB default line limit.
            proc = await self.process_factory(*args, stdin=asyncio.subprocess.PIPE,
                                              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                              limit=REPLY_LIMIT)
        except (FileNotFoundError, OSError) as e:
            raise sandbox.SandboxUnavailable(f"Docker did not start: {e}") from e
        proc.stdin.write((json.dumps({"driver": SOURCE}) + "\n").encode())
        c = Computer(owner, proc, name, desktop=desktop, last_action=self.clock())
        c.stderr_task = asyncio.create_task(self._drain_stderr(proc))
        c.reader_task = asyncio.create_task(self._read(c))
        return c

    async def _read(self, c: Computer):
        """Route each stdout line: a frame to the live view, a reply to its command."""
        try:
            while line := await c.proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("event") == "step":
                    if (c.taken_over and c.recording and message.get("recording_id") == c.recording_id
                            and len(c.steps) < RECORDING_LIMIT and isinstance(message.get("step"), dict)):
                        c.steps.append(message["step"])
                    continue
                if message.get("event") == "frame":
                    try:
                        c.frame_jpeg = base64.b64decode(message["data"])
                    except (KeyError, ValueError):
                        continue
                    c.frame_seq += 1
                    changed, c.frame_changed = c.frame_changed, asyncio.Event()
                    changed.set()
                    continue
                waiter = c.pending.pop(message.get("id"), None)
                if waiter and not waiter.done():
                    waiter.set_result(message)
        finally:
            c.recording, c.recording_id = False, None
            c.steps.clear()
            for waiter in c.pending.values():
                if not waiter.done():
                    waiter.set_exception(sandbox.SandboxUnavailable("the contained computer stopped"))
            c.pending.clear()
            c.frame_changed.set()

    async def _get(self, owner: str, is_admin: bool = True) -> Computer:
        async with self._lock:
            await self._close_idle()
            if owner not in self._computers:
                if len(self._computers) >= MAX_RUNNING:
                    oldest = min(self._computers.values(), key=lambda c: c.last_action)
                    await self._stop(oldest.owner)
                self._computers[owner] = await self._start(owner, is_admin=is_admin)
                self.forget(owner)
                if self._idle_task is None or self._idle_task.done():
                    self._idle_task = asyncio.create_task(self._idle_loop())
            return self._computers[owner]

    async def _command(self, c: Computer, action: str, **kwargs) -> dict:
        if action.startswith(("watch_", "record_")):
            return await self._send(c, action, **kwargs)
        async with c.lock:
            return await self._send(c, action, **kwargs)

    async def _send(self, c: Computer, action: str, **kwargs) -> dict:
        if c.reader_task is None or c.reader_task.done():
            raise sandbox.SandboxUnavailable("the contained computer stopped")
        c.sequence += 1
        ident = c.sequence
        waiter = asyncio.get_running_loop().create_future()
        c.pending[ident] = waiter
        c.proc.stdin.write((json.dumps({"id": ident, "action": action, **kwargs}) + "\n").encode())
        await c.proc.stdin.drain()
        try:
            reply = await asyncio.wait_for(waiter, 45)
        except asyncio.TimeoutError as e:
            raise sandbox.SandboxUnavailable("the contained computer did not answer") from e
        finally:
            c.pending.pop(ident, None)
        if reply.get("error"):
            raise ValueError(reply["error"])
        return reply

    async def _stop(self, owner: str):
        c = self._computers.pop(owner, None)
        if c is None:
            return
        from core.computer_history import jpeg
        image = jpeg(c.screenshot) if c.screenshot else None
        self._closed[owner] = (self.clock() + IDLE_SECONDS, {
            "owner": owner, "url": c.url, "title": c.title, "last_action": c.last_action_at,
            "closed_at": time.time(), "taken_over": False, "waiting_model": False,
            "desktop": c.desktop, "recording": False,
            "image": base64.b64encode(image).decode() if image else "",
        })
        c.taken_over = False
        self._discard_recording(c)
        c.handback.set()
        try:
            c.proc.stdin.close()
            await asyncio.wait_for(c.proc.wait(), 2)
        except (OSError, asyncio.TimeoutError):
            try:
                c.proc.kill()
                await c.proc.wait()
            except ProcessLookupError:
                pass
        for task in (c.stderr_task, c.reader_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def stop(self, owner: str):
        async with self._lock:
            await self._stop(owner)

    async def close_all(self):
        async with self._lock:
            for owner in list(self._computers):
                await self._stop(owner)
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None
        self._closed.clear()

    async def _close_idle(self):
        for c in list(self._computers.values()):
            if not c.waiting_model and self.clock() - c.last_action >= IDLE_SECONDS:
                await self._stop(c.owner)

    async def _idle_loop(self):
        try:
            while self._computers or self._closed:
                await asyncio.sleep(30)
                async with self._lock:
                    await self._close_idle()
                    self._expire_frames()
        except asyncio.CancelledError:
            pass

    async def _site(self, c: Computer, host: str, ctx) -> tuple[bool, str]:
        if host in c.hosts:
            return True, ""
        decision = await permissions.decide(surface=ctx.permission_surface, tool="computer_site",
            arguments={"url": f"https://{host}"}, target=host,
            title=f"Let the computer open {host}", description=f"Open {host} in the contained computer.",
            is_admin=ctx.is_admin)
        if decision.behavior != "allow":
            return False, decision.reason
        c.hosts.add(host)
        return True, ""

    async def _retreat(self, c: Computer):
        try:
            previous = await self._command(c, "back")
            url = previous.get("url") or "about:blank"
            host = urlsplit(url).hostname
            if (host and host.lower() not in c.hosts) or (not host and url != "about:blank"):
                await self.stop(c.owner)
                return
            c.url, c.title = url, previous.get("title") or ""
            if previous.get("screenshot"):
                c.screenshot = base64.b64decode(previous["screenshot"])
        except (ValueError, sandbox.SandboxUnavailable):
            await self.stop(c.owner)

    async def act(self, owner: str, action: str, args: dict, ctx) -> dict:
        existing = self._computers.get(owner)
        if existing and existing.taken_over:
            existing.waiting_model = True
            try:
                deadline = self.clock() + IDLE_SECONDS
                while existing.taken_over and self._computers.get(owner) is existing:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        return {"text": "Not run: the person has control of the computer.", "images": []}
                    try:
                        await asyncio.wait_for(existing.handback.wait(), min(remaining, 0.25))
                    except asyncio.TimeoutError:
                        pass
                if self._computers.get(owner) is not existing:
                    return {"text": "Not run: the computer was stopped.", "images": []}
            finally:
                existing.waiting_model = False
        if action == "done":
            previous = self._computers.get(owner)
            url = previous.url if previous else "about:blank"
            await self.stop(owner)
            return {"text": f"URL: {url} | Computer closed.", "images": []}
        wants_desktop = action in ("launch", "windows") or args.get("desktop") is True
        if wants_desktop and not (existing.desktop if existing else _desktop_enabled() and ctx.is_admin):
            return {"text": "Not run: the agent desktop is off. An admin can switch it on in Settings > Computer use; the change applies at the next start.", "images": []}
        c = await self._get(owner, is_admin=ctx.is_admin)
        if c.desktop and not ctx.is_admin:
            return {"text": "Not run: the agent desktop is limited to admins.", "images": []}
        desktop_action = wants_desktop or action == "screenshot" and c.desktop and args.get("desktop") is not False
        if desktop_action and action not in ("launch", "windows", "click", "type", "key", "scroll", "screenshot"):
            return {"text": "Not run: desktop coordinates apply to click, type, key, scroll or screenshot.", "images": []}
        command = {}
        if action != "open":
            current = await self._command(c, "state")
            url = current.get("url") or "about:blank"
            host = urlsplit(url).hostname
            if urlsplit(url).scheme not in ("http", "https") and url != "about:blank":
                await self._retreat(c)
                return {"text": f"Not opened: the computer only opens web addresses. URL: {url}", "images": []}
            if host:
                allowed, reason = await self._site(c, host.lower(), ctx)
                if not allowed:
                    await self._retreat(c)
                    return {"text": f"Not opened: {reason} URL: {url}", "images": []}
            c.url, c.title = url, current.get("title") or ""
            command["expected_url"] = url
        if desktop_action:
            try:
                return await self._desktop_act(c, action, args, ctx)
            except sandbox.SandboxUnavailable:
                await self.stop(owner)
                raise
        if action == "open":
            url = sandbox_browser._check_url(args.get("url") or "")
            if urlsplit(url).username or urlsplit(url).password:
                raise ValueError("addresses with sign-in details are not allowed")
            host = urlsplit(url).hostname.lower()
            allowed, reason = await self._site(c, host, ctx)
            if not allowed:
                return {"text": f"Not opened: {reason}", "images": []}
            command["url"] = url
        elif action in ("click", "type"):
            command.update({k: args[k] for k in ("x", "y", "ref") if k in args})
            info = (await self._command(c, "inspect", **command)).get("element") or {}
            if action == "type" and not info:
                info = (await self._command(c, "focused", expected_url=c.url)).get("element") or {}
            if info.get("opaque_frame"):
                _needs_frame_person(owner, c.url)
                return {"text": _opaque_frame_message() if action == "click" else
                        f"Not typed: this embedded frame cannot be inspected. Ask the person to take over. URL: {c.url}",
                        "images": []}
            if action == "type" and _private(info):
                _needs_person(owner, c.url)
                return {"text": "Not typed: passwords, codes and payment details are entered by the person. Ask them to take over the computer. "
                        f"URL: {c.url}", "images": []}
            if action == "click":
                stopped = await self._press_guard(c, info, ctx)
                if stopped:
                    return {"text": stopped, "images": []}
            command["expected"] = info
            if action == "type": command["text"] = args.get("text") or ""
        elif action == "key":
            command["key"] = args.get("key") or ""
            info = (await self._command(c, "focused", expected_url=c.url)).get("element") or {}
            if info.get("opaque_frame"):
                _needs_frame_person(owner, c.url)
                return {"text": f"Not pressed: this embedded frame cannot be inspected. Ask the person to take over. URL: {c.url}",
                        "images": []}
            command["expected"] = info
            if _private(info) and command["key"].lower() not in ("tab", "shift+tab", "escape"):
                _needs_person(owner, c.url)
                return {"text": "Not typed: passwords, codes and payment details are entered by the person. Ask them to take over the computer. "
                        f"URL: {c.url}", "images": []}
            key = command["key"].lower().split("+")[-1]
            if key in ("enter", "return", "numpadenter", "space", "spacebar"):
                stopped = await self._press_guard(c, info, ctx, enter=key in ("enter", "return", "numpadenter"))
                if stopped:
                    return {"text": stopped, "images": []}
        elif action == "scroll":
            command.update(dx=args.get("dx", 0), dy=args.get("dy", 0))
        elif action == "wait":
            command["seconds"] = min(10, max(0, float(args.get("seconds", 1))))
        elif action not in ("screenshot", "read", "back"):
            return {"text": f"Not run: unknown computer action {action}", "images": []}
        try:
            reply = await self._command(c, action, **command)
        except sandbox.SandboxUnavailable:
            await self.stop(owner)
            raise
        c.last_action = self.clock()
        c.last_action_at = time.time()
        new_url = reply.get("url") or c.url
        if urlsplit(new_url).scheme not in ("http", "https") and new_url != "about:blank":
            await self._retreat(c)
            return {"text": f"Not opened: the computer only opens web addresses. URL: {c.url}", "images": []}
        new_host = urlsplit(new_url).hostname
        if new_host and new_host.lower() not in c.hosts:
            allowed, reason = await self._site(c, new_host.lower(), ctx)
            if not allowed:
                await self._retreat(c)
                return {"text": f"Not opened: {reason}", "images": []}
        c.url, c.title = new_url, reply.get("title") or ""
        c.screenshot = base64.b64decode(reply["screenshot"])
        detail = f"URL: {c.url} | Title: {c.title} | {action} completed."
        if action == "read":
            entries = [f"[{e['ref']}] {e.get('tag','')} {e.get('label','')[:120]} {e.get('href','')}".strip()
                       for e in reply.get("elements", [])]
            detail += "\n\n" + reply.get("text", "") + "\n\nElements:\n" + ("\n".join(entries) or "(none)")
        if ctx.turn_taint:
            ctx.turn_taint.mark("computer page")
        return {"text": detail, "images": [c.screenshot]}

    async def _desktop_act(self, c: Computer, action: str, args: dict, ctx) -> dict:
        if action == "launch" and args.get("app") not in computer_image.DESKTOP_APPS:
            return {"text": "Not run: choose files, editor, pdf, images, writer, calc or impress.", "images": []}
        async with c.lock:
            if action in ("click", "type", "key", "scroll"):
                values = {k: args[k] for k in ("x", "y", "text", "key", "dx", "dy") if k in args}
                if action in ("click", "scroll"):
                    info = await self._send(c, "window_at", expected_url=c.url, **{k: values[k] for k in ("x", "y") if k in values})
                else:
                    info = await self._send(c, "focused_window", expected_url=c.url)
                info = {k: (info.get("window") or {}).get(k, "") for k in ("id", "class", "title")}
                if any(word in info["class"].lower() for word in ("chromium", "chrome", "kairos-computer-browser")):
                    return {"text": "Not run: That is the browser window: use the computer's web actions (open, read, click by ref) so its safety checks apply.", "images": []}
                if not info["class"]:
                    return {"text": "Not run: could not identify the desktop window; ask the person to take over.", "images": []}
                if c.taken_over:
                    return {"text": "Not run: the person has control of the computer.", "images": []}
                reply = await self._send(c, "desktop_input", kind=action, expected_window=info, expected_url=c.url, **values)
            else:
                reply = await self._send(c, "desktop_screenshot" if action == "screenshot" else action,
                                         expected_url=c.url, **({"app": args["app"]} if action == "launch" else {}))
        c.last_action = self.clock()
        c.last_action_at = time.time()
        new_url = reply.get("url") or c.url
        if urlsplit(new_url).scheme not in ("http", "https") and new_url != "about:blank":
            await self._retreat(c)
            return {"text": f"Not opened: the computer only opens web addresses. URL: {c.url}", "images": []}
        new_host = urlsplit(new_url).hostname
        if new_host and new_host.lower() not in c.hosts:
            allowed, reason = await self._site(c, new_host.lower(), ctx)
            if not allowed:
                await self._retreat(c)
                return {"text": f"Not opened: {reason}", "images": []}
        c.url, c.title = new_url, reply.get("title") or ""
        c.screenshot = base64.b64decode(reply["screenshot"])
        detail = f"URL: {c.url} | Desktop {action} completed."
        if action == "windows": detail += "\n" + json.dumps(reply.get("windows", []))
        if ctx.turn_taint: ctx.turn_taint.mark("computer desktop")
        return {"text": detail, "images": [c.screenshot]}

    def takeover(self, owner: str) -> bool:
        c = self._computers.get(owner)
        if not c:
            return False
        c.taken_over = True
        c.handback.clear()
        c.last_action = self.clock()
        c.last_action_at = time.time()
        self._rate_soon(c)
        return True

    def _rate_soon(self, c: Computer) -> None:
        """Control changed: re-rate the screencast (takeover and hand_back are sync)."""
        try:
            asyncio.get_running_loop().create_task(self._sync_rate(c))
        except RuntimeError:
            pass

    def hand_back(self, owner: str) -> bool:
        c = self._computers.get(owner)
        if not c:
            return False
        c.taken_over = False
        self._discard_recording(c)
        c.handback.set()
        c.last_action = self.clock()
        c.last_action_at = time.time()
        self._rate_soon(c)
        return True

    def _discard_recording(self, c: Computer) -> None:
        ident, c.recording_id = c.recording_id, None
        c.recording = False
        c.steps.clear()
        if ident and self._computers.get(c.owner) is c:
            async def discard():
                try:
                    await self._command(c, "record_stop", recording_id=ident)
                except (ValueError, sandbox.SandboxUnavailable):
                    pass
            asyncio.get_running_loop().create_task(discard())

    async def start_recording(self, owner: str) -> None:
        c = self._computers.get(owner)
        if not c or not c.taken_over:
            raise ValueError("take over the computer before recording")
        async with c.lock:
            if not c.taken_over or self._computers.get(owner) is not c:
                raise ValueError("take over the computer before recording")
            if c.recording:
                raise ValueError("recording is already on")
            c.steps.clear()
            c.recording, c.recording_id = True, uuid.uuid4().hex
            try:
                await self._command(c, "record_start", recording_id=c.recording_id)
            except Exception:
                self._discard_recording(c)
                raise

    async def stop_recording(self, owner: str) -> list[dict]:
        c = self._computers.get(owner)
        if not c or not c.taken_over or not c.recording:
            raise ValueError("there is no recording to review")
        async with c.lock:
            if not c.taken_over or not c.recording:
                raise ValueError("there is no recording to review")
            await self._command(c, "record_stop", recording_id=c.recording_id)
            steps = c.steps[:]
            c.recording, c.recording_id = False, None
            c.steps.clear()
            return steps

    async def person_input(self, owner: str, kind: str, values: dict) -> None:
        """Forward human input without model inspection, tool output or audit."""
        c = self._computers.get(owner)
        if not c or not c.taken_over:
            raise ValueError("take over the computer first")
        async with c.lock:
            if not c.taken_over or self._computers.get(owner) is not c:
                raise ValueError("take over the computer first")
            reply = await self._send(c, "desktop_input", kind=kind, person=True, recording_id=c.recording_id, **values) if c.desktop else await self._send(c, "person_" + kind, recording_id=c.recording_id, **values)
        c.url, c.title = reply.get("url") or c.url, reply.get("title") or c.title
        if reply.get("screenshot"):
            c.screenshot = base64.b64decode(reply["screenshot"])
        c.last_action = self.clock()
        c.last_action_at = time.time()

    async def frame(self, owner: str) -> dict | None:
        """Refresh a watched page at most twice a second."""
        c = self._computers.get(owner)
        if not c:
            return None
        async with c.snapshot_lock:
            if self.clock() - c.last_snapshot >= 0.5:
                reply = await self._command(c, "snapshot")
                c.last_snapshot = self.clock()
                c.url, c.title = reply.get("url") or c.url, reply.get("title") or c.title
                c.frame_jpeg = base64.b64decode(reply["screenshot"])
                c.screenshot = c.frame_jpeg
        if self._computers.get(owner) is not c:
            return None
        return {"owner": owner, "url": c.url, "title": c.title, "last_action": c.last_action_at,
                "desktop": c.desktop, "recording": c.recording,
                "taken_over": c.taken_over, "waiting_model": c.waiting_model,
                "image": base64.b64encode(c.frame_jpeg).decode() if c.frame_jpeg else ""}

    def _wanted_fps(self, c: Computer) -> int:
        return CONTROL_FPS if c.taken_over and c.local_watchers else WATCH_FPS

    async def _sync_rate(self, c: Computer) -> None:
        """Tell the driver the screencast rate the viewers and control call for."""
        want = self._wanted_fps(c)
        if c.watchers and want != c.cast_fps and self._computers.get(c.owner) is c:
            try:
                await self._command(c, "watch_rate", fps=want)
                c.cast_fps = want
            except (ValueError, sandbox.SandboxUnavailable):
                pass

    async def watch(self, owner: str, heartbeat: float = 1.0, local: bool = False):
        """The live view: every screencast frame as it arrives, and the state
        (control, URL) at least once a `heartbeat`, without an image when the
        page has not changed. The screencast runs only while someone watches.
        A `local` viewer (this computer, not Remote Access) gets CONTROL_FPS
        while the person has control; everyone else is capped at WATCH_FPS."""
        c = self._computers.get(owner)
        if not c:
            return
        c.watchers += 1
        c.local_watchers += local
        last_image = 0.0
        try:
            if c.watchers == 1:
                try:
                    c.cast_fps = self._wanted_fps(c)
                    await self._command(c, "watch_start", fps=c.cast_fps)
                except (ValueError, sandbox.SandboxUnavailable):
                    pass
            else:
                await self._sync_rate(c)
            if c.frame_jpeg is None:
                await self.frame(owner)  # a still page sends no screencast frame: take one
            # Whatever is on screen now goes first, even if it arrived before
            # this viewer did; after that, only new frames carry an image.
            sent = c.frame_seq
            if c.frame_jpeg and self._computers.get(owner) is c:
                last_image = time.monotonic()
                yield {"owner": owner, "url": c.url, "title": c.title, "last_action": c.last_action_at,
                       "desktop": c.desktop, "recording": c.recording,
                       "taken_over": c.taken_over, "waiting_model": c.waiting_model,
                       "image": base64.b64encode(c.frame_jpeg).decode()}
            while self._computers.get(owner) is c:
                changed = c.frame_changed
                try:
                    await asyncio.wait_for(changed.wait(), heartbeat)
                except asyncio.TimeoutError:
                    pass
                if self._computers.get(owner) is not c:
                    return
                if c.frame_seq != sent and c.frame_jpeg:
                    # This viewer's own cap: a Remote Access viewer stays at
                    # WATCH_FPS even while a local one gets CONTROL_FPS.
                    fps = CONTROL_FPS if local and c.taken_over else WATCH_FPS
                    wait = 1 / fps - (time.monotonic() - last_image)
                    if wait > 0:
                        await asyncio.sleep(wait)
                        if self._computers.get(owner) is not c:
                            return
                state = {"owner": owner, "url": c.url, "title": c.title, "last_action": c.last_action_at,
                         "desktop": c.desktop, "recording": c.recording,
                         "taken_over": c.taken_over, "waiting_model": c.waiting_model}
                if c.frame_seq != sent and c.frame_jpeg:
                    sent = c.frame_seq
                    last_image = time.monotonic()
                    state["image"] = base64.b64encode(c.frame_jpeg).decode()
                yield state
        finally:
            c.watchers -= 1
            c.local_watchers -= local
            if self._computers.get(owner) is c:
                if c.watchers == 0:
                    try:
                        await self._command(c, "watch_stop")
                    except (ValueError, sandbox.SandboxUnavailable):
                        pass
                    c.cast_fps = 0
                else:
                    await self._sync_rate(c)

    async def _press_guard(self, c: Computer, info: dict, ctx, enter: bool = False) -> str | None:
        controls = [info, *((info.get("form_submits") or []) if enter else [])]
        host = urlsplit(c.url).hostname or "this page"

        def hand_over(shown: str, why: str) -> str:
            if c.owner.startswith("agent:"):
                from services.agent_service import agent_service
                agent_service.add_item(c.owner.removeprefix("agent:"), kind="question",
                    title=f"Ready for you: press '{shown}' at {host}", body=f"The computer is ready at {c.url}. Take over and press it.")
            return (f"Stopped before pressing '{shown}' on {host}: {why} "
                    "Everything up to it is ready; ask them to take over and press it.")

        for control in controls:
            for label in _labels(control):
                if PERSON_ACTION_WORDS.search(label):
                    return hand_over(label[:160], "buying, sending and posting are done by the person.")
        if not _reactions_allowed():
            for control in controls:
                for label in _labels(control):
                    if REACTION_WORDS.search(label):
                        return hand_over(label[:160], "likes, follows and reactions are done by the person unless "
                                                      "an admin allows them in Settings > Computer use.")
        for control in controls:
            for label in _labels(control):
                if DELETE_WORDS.search(label):
                    shown = label[:160]
                    decision = await permissions.decide(surface=ctx.permission_surface, tool="computer_delete",
                        target=None, arguments={"url": c.url, "label": shown},
                        title=f"Let the computer press '{shown}' on {host}",
                        description=f"Press '{shown}' on {c.url}", choices=DELETE_CHOICES, is_admin=ctx.is_admin)
                    return None if decision.behavior == "allow" else f"Not pressed: {decision.reason} URL: {c.url}"
        return None


def _desktop_enabled() -> bool:
    from core import settings
    return bool((settings.get_setting("computer_use") or {}).get("desktop"))


def _reactions_allowed() -> bool:
    from core import settings
    return bool((settings.get_setting("computer_use") or {}).get("allow_reactions"))


def _labels(info: dict) -> list[str]:
    labels = info.get("signals") or [info.get("label") or ""]
    return [str(label).strip() for label in labels if str(label).strip()]


def _private(info: dict) -> bool:
    autocomplete = info.get("autocomplete") or ""
    return (info.get("tag") == "input" and info.get("type") == "password") or autocomplete in PRIVATE_FIELDS or autocomplete.startswith("cc-")


def _needs_person(owner: str, url: str):
    if owner.startswith("agent:"):
        from services.agent_service import agent_service
        host = urlsplit(url).hostname or "this site"
        agent_service.add_item(owner.removeprefix("agent:"), kind="question",
            title=f"Needs you: sign in at {host}", body=f"The contained computer is waiting at {url}. Take over to enter private details.")


def _opaque_frame_message() -> str:
    return "Stopped: this part of the page is an embedded frame Kairos can't inspect (often a payment form). Ask the person to take over."


def _needs_frame_person(owner: str, url: str):
    if owner.startswith("agent:"):
        from services.agent_service import agent_service
        host = urlsplit(url).hostname or "this site"
        agent_service.add_item(owner.removeprefix("agent:"), kind="question",
            title=f"Needs you: embedded frame at {host}", body=f"The contained computer is waiting at {url}. Take over to use this frame.")


manager = ComputerManager()


async def stop(owner: str):
    await manager.stop(owner)


def running() -> list[dict]:
    return manager.running()
