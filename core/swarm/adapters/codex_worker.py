"""Codex CLI transport for Swarm.

Not admitted for autonomous work in C1, and the reason is structural rather
than a preference: the installed `codex exec` takes structured tools only
through MCP servers it launches itself, so a Swarm tool surface for Codex
needs the authenticated per-attempt worker bridge described in section 10 of
the implementation spec. Until that exists, `capability_for` reports no
structured tools and admission blocks this kind with a specific reason. A
worker that can talk but cannot act must not be presented as a working agent.

What is real here is the transport: a bounded step, parsed token usage, and a
cancellation that kills the whole process tree and says plainly when it could
not confirm the tree is gone. That honesty matters more than the feature -
`core/codex_brain.py` kills only the direct child, which on Windows leaves
grandchildren running.

Two things `core/codex_brain.py` does are deliberately not done here: the
process-wide internal token is never placed in the child environment, and
continuity is never written into the app's SessionManager.
"""
import asyncio
import json
import os
import shutil
import sys
import uuid

from . import role_prompt, task_prompt
from ..models import EventKind, WorkerEvent

MESSAGE_TIMEOUT_SECONDS = 180
STOP_GRACE_SECONDS = 5


class CodexWorker:
    def __init__(self, context):
        self.context = context
        self.process = None
        self.stopped = None

    def _args(self, codex):
        args = [codex, "exec", "--json", "--skip-git-repo-check",
                "-s", "workspace-write", "-C", self.context.scratch_dir]
        if self.context.effort:
            args[2:2] = ["-c", f'model_reasoning_effort="{self.context.effort}"']
        model = self.context.model or (self.context.endpoint or {}).get("model")
        if model:
            args += ["-m", model]
        args.append("-")
        return args

    async def events(self, assignment):
        self.context.tool_service.bind(assignment)
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("Codex CLI not found on PATH.")
        prompt = (f"[System instructions:]\n{role_prompt(self.context)}\n\n"
                  f"[Task:]\n{task_prompt(assignment.objective)}")
        # A clean environment: no internal token, no session id, nothing this
        # worker was not explicitly granted.
        env = {**os.environ, **dict(self.context.env)}
        env.pop("JARVIS_INTERNAL_TOKEN", None)
        env.pop("JARVIS_CODEX_SESSION_ID", None)
        self.process = await asyncio.create_subprocess_exec(
            *self._args(codex), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env)
        self.process.stdin.write(prompt.encode("utf-8"))
        self.process.stdin.write_eof()
        usage_reported = False
        text_seen = []
        try:
            while True:
                try:
                    line = await asyncio.wait_for(self.process.stdout.readline(),
                                                  timeout=MESSAGE_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    raise RuntimeError("Codex stopped responding")
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                    text = event["item"].get("text") or ""
                    if text.strip():
                        text_seen.append(text)
                        yield WorkerEvent(EventKind.TEXT, uuid.uuid4().hex, {"text": text})
                elif kind == "turn.completed":
                    usage = event.get("usage") or {}
                    units = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                    if units:
                        usage_reported = True
                        yield WorkerEvent(EventKind.USAGE, f"{assignment.attempt_id}:turn", {"units": units})
                elif kind in ("error", "turn.failed"):
                    message = event.get("message") or (event.get("error") or {}).get("message")
                    raise RuntimeError(message or "Codex could not complete this turn")
            yield WorkerEvent(EventKind.RESULT, f"{assignment.attempt_id}:final", {
                "result": {"status": "incomplete",
                           "reason": "This connection has no Swarm tool surface, so the step could only produce text.",
                           "output": "\n\n".join(text_seen)[:8000]},
                "usage_complete": usage_reported,
            })
        finally:
            await self._terminate()

    async def _terminate(self):
        process = self.process
        if process is None or process.returncode is not None:
            self.stopped = True if process is None or process.returncode is not None else self.stopped
            return
        # Kill the tree, not the direct child. A codex process spawns its own
        # helpers; terminating only the parent leaves them running and the
        # attempt would be recorded as stopped when it is not.
        confirmed = False
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            confirmed = await killer.wait() == 0
        else:
            try:
                os.killpg(os.getpgid(process.pid), 9)
                confirmed = True
            except (ProcessLookupError, PermissionError, OSError):
                confirmed = False
        try:
            await asyncio.wait_for(process.wait(), timeout=STOP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            confirmed = False
        self.stopped = confirmed

    async def cancel(self) -> bool:
        await self._terminate()
        return bool(self.stopped)
