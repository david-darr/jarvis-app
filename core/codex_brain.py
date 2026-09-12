"""Channel-agnostic turn handler for a session pinned to the `codex_cli`
model endpoint kind (core/model_endpoints.py) — same connect()/run_turn()/
run_turn_stream()/disconnect() shape as core/brain.py's Brain, so
services/chat_service.py can pick either implementation per session without
its own call sites caring which one they're talking to.

Unlike Brain (a long-lived Claude Agent SDK connection kept open across
turns), `codex exec` is a one-shot-per-invocation CLI: every turn spawns a
fresh `codex` process that exits when the turn is done. Continuity across
turns comes from Codex's own server-side thread history, resumed by id
(`codex exec resume <thread_id>`) rather than anything held in this Python
process — verified live before writing this: resuming the *original*
thread_id on every later turn (not whatever id that resume call itself
re-emits in its own thread.started event) correctly recalls earlier
context. The thread_id is persisted onto the session record
(session_manager.set_codex_thread_id), not just held in memory, so this
survives an app restart — real continuity Brain's Claude-CLI path doesn't
have (see chat_service.py's _prime_with_history docstring: the Claude Agent
SDK exposes no equivalent resume-by-id mechanism, hence that text-replay
workaround).

Verified live, not guessed:
- `codex exec --json` / `codex exec resume <id> --json` emit clean JSONL
  events on stdout (thread.started, item.completed, turn.completed) —
  parsed directly here, no SDK needed.
- `-s read-only` hangs forever in a non-interactive context (some action
  needs an approval prompt nothing can answer headlessly) — `workspace-write`
  is the only sandbox mode used here, matching Claude's own acceptEdits
  posture.
- `codex exec resume` does not accept `-C`/`--sandbox` — it already knows
  its working directory and mode from the original invocation, so those
  flags are only passed on the first (non-resume) call.
- Passing the prompt via stdin (`-` as the positional arg) avoids all
  Windows shell-quoting concerns entirely — no argument ever needs escaping.

Tool-surface note: Codex has no equivalent to core/hive_mind_server.py yet
(that's an in-process Claude Agent SDK construct; Codex's `codex mcp add`
only accepts external stdio/HTTP servers) — this is Phase 1, chat-endpoint
parity only. core/system_prompt.py's for_codex() is deliberately honest
about that gap rather than promising tools that don't exist.
"""
import asyncio
import json
import shutil

from core.constants import REPO_CODE_DIRS
from core.session_manager import session_manager
from core.vault import resolve_vault_dir
from core import system_prompt

# Per-message timeout, not per-turn (a real tool-call chain can legitimately
# take a while) — same posture and value as core/brain.py's
# TURN_MESSAGE_TIMEOUT_SECONDS, so a hung/crashed codex process doesn't spin
# the chat UI forever with no error.
CODEX_MESSAGE_TIMEOUT_SECONDS = 180


class CodexBrain:
    """One CodexBrain per conversation session. Create one, call connect()
    once (a no-op — nothing to keep open between turns), then run_turn(text)/
    run_turn_stream(text) per incoming message, and disconnect() (also a
    no-op) on shutdown."""

    def __init__(self, vault_dir: str | None = None, cwd_override: str | None = None,
                 session_id: str | None = None, model: str | None = None, is_admin: bool = False):
        self.cwd = cwd_override or vault_dir or resolve_vault_dir()
        self.model = model
        self.session_id = session_id
        self.is_admin = is_admin
        self.last_usage: dict | None = None
        session = session_manager.get_session(session_id) if session_id else None
        # Present only once this session has completed at least one codex_cli
        # turn before — see set_codex_thread_id's caller below.
        self.thread_id: str | None = (session or {}).get("codex_thread_id")

    @staticmethod
    def _codex_path() -> str:
        path = shutil.which("codex")
        if not path:
            raise RuntimeError(
                "Codex CLI not found on PATH. Install it with `npm install -g @openai/codex` "
                "and run `codex login`, then try again."
            )
        return path

    def _build_args(self, codex: str) -> list[str]:
        if self.thread_id:
            # Resume doesn't take -C/-s — the original invocation already
            # fixed the working directory and sandbox mode for this thread.
            args = [codex, "exec", "resume", self.thread_id, "--json", "--skip-git-repo-check"]
        else:
            args = [codex, "exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-C", self.cwd]
            # Repo dev access mirrors core/brain.py's add_dirs=REPO_CODE_DIRS
            # for Claude — but unlike Claude (separate file tools vs. a
            # gateable Bash tool), Codex has one native tool surface whose
            # scope IS the sandbox boundary, so admin-gating the directory
            # grant itself is the only equivalent lever available: a
            # non-admin session's shell/file access stays confined to the
            # vault/workspace, same ceiling Claude's non-admin Bash-denial
            # produces by a different mechanism.
            if self.is_admin:
                for extra_dir in REPO_CODE_DIRS:
                    args += ["--add-dir", extra_dir]
        if self.model:
            args += ["-m", self.model]
        args.append("-")  # read the prompt from stdin, never as an argv string
        return args

    async def connect(self) -> None:
        pass  # nothing to keep open — each turn is its own process

    async def run_turn(self, user_text: str) -> str:
        parts = [chunk async for chunk in self.run_turn_stream(user_text)]
        return "".join(parts).strip()

    async def run_turn_stream(self, user_text: str):
        codex = self._codex_path()
        is_fresh_thread = self.thread_id is None
        args = self._build_args(codex)

        # The landing-zone prompt (see core/system_prompt.py) only needs
        # sending once — codex's own server-side thread history carries it
        # on every later resumed turn, same one-time-per-connection idea as
        # core/brain.py's system_prompt (there, "per connection" is the
        # right granularity because a fresh SDK connection has no memory of
        # prior instructions either).
        if is_fresh_thread:
            prompt = f"[System instructions:]\n{system_prompt.for_codex(self.is_admin)}\n\n[User message:]\n{user_text}"
        else:
            prompt = user_text

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.write_eof()

        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=CODEX_MESSAGE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                yield (
                    "\n\n[JARVIS: Codex didn't respond within "
                    f"{CODEX_MESSAGE_TIMEOUT_SECONDS}s and may be stuck — try sending "
                    "your message again.]"
                )
                return
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # codex occasionally logs non-JSON noise lines; ignore rather than fail the turn

            etype = event.get("type")
            if etype == "thread.started" and self.thread_id is None:
                self.thread_id = event["thread_id"]
                if self.session_id:
                    session_manager.set_codex_thread_id(self.session_id, self.thread_id)
            elif etype == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                yield event["item"]["text"]
            elif etype == "turn.completed":
                usage = event.get("usage") or {}
                # input_tokens/output_tokens are the true, non-overlapping
                # totals; cached_input_tokens/reasoning_output_tokens are
                # subsets of those two (verified live), so summing every
                # *_tokens key the way core/token_usage.py's generic Claude
                # path does would double-count. Pre-summed here instead.
                self.last_usage = {"total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)}
            elif etype == "error":
                yield f"\n\n[JARVIS: Codex error — {event.get('message', 'unknown error')}]"

        rc = await proc.wait()
        if rc != 0:
            stderr = (await proc.stderr.read()).decode(errors="replace")[:500].strip()
            # Self-heal a stale resume target (verified live: codex reports
            # exactly this "no rollout found" message when a persisted
            # thread_id has since been archived/deleted/pruned on the codex
            # side) — without this, every future turn in this session would
            # keep resuming the same dead id and fail the same way forever.
            # Clear it and retry once as a brand-new thread rather than
            # leaving the session permanently stuck; only fires when this
            # was actually a resume attempt, so a genuinely fresh thread
            # that fails for some other reason isn't retried into a loop.
            if not is_fresh_thread and "no rollout found" in stderr.lower():
                self.thread_id = None
                if self.session_id:
                    session_manager.set_codex_thread_id(self.session_id, None)
                async for chunk in self.run_turn_stream(user_text):
                    yield chunk
                return
            yield f"\n\n[JARVIS: Codex exited with an error (code {rc}) — {stderr or 'no output'}]"

    async def disconnect(self) -> None:
        pass  # no persistent process to close
