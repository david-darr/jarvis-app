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
survives an app restart. The Claude path now does the same with the Claude
Agent SDK's own `resume` option (session_manager.set_claude_session); this
note used to say that SDK had no resume-by-id, which stopped being true, and
the whole-transcript replay that claim justified cost a full prompt-cache
miss on every reconnect.

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
import os
import shutil
import sys

from core.auth import INTERNAL_TOOL_TOKEN
from core import image_gen
from core.constants import BASE_DIR, REPO_CODE_DIRS
from core.session_manager import session_manager
from core.vault import resolve_vault_dir
from core import projects, system_prompt

# Phase 2 (2026-09-11): the CLI wrapper hive-mind tools are invoked through
# (see core/system_prompt.py's for_codex() and mcp_servers/hive_mind_cli.py's
# own docstring for why this replaced an MCP server). sys.executable is
# exactly the venv python already running this backend process — no
# separate path-guessing needed, unlike resolving the `codex` binary itself.
HIVE_MIND_CLI_PATH = os.path.join(BASE_DIR, "mcp_servers", "hive_mind_cli.py")

# Per-message timeout, not per-turn (a real tool-call chain can legitimately
# take a while) — same posture and value as core/brain.py's
# TURN_MESSAGE_TIMEOUT_SECONDS, so a hung/crashed codex process doesn't spin
# the chat UI forever with no error.
CODEX_MESSAGE_TIMEOUT_SECONDS = 180


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill proc and everything it spawned.

    `codex exec` on Windows is a process tree (node -> codex.exe -> a cua-repl
    node/node_repl pair -> codex-code-mode-host.exe), not a single process.
    Windows does not cascade-kill children when a parent dies, so a plain
    proc.kill() on timeout only kills the top node.exe and orphans the rest —
    verified live: two timed-out turns left 5-process trees still running and
    holding their thread's ~/.codex/thread-writer-locks/<id>.lock file over
    8 hours later, permanently wedging that session (every future resume on
    the same thread_id fails to acquire the lock). `taskkill /T /F` kills the
    whole tree in one call; proc.kill() is the fallback for non-Windows or if
    taskkill itself is unavailable.
    """
    if proc.returncode is not None:
        return
    if sys.platform == "win32" and proc.pid:
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/T", "/F", "/PID", str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    if proc.returncode is None:
        proc.kill()
    await proc.wait()


class CodexBrain:
    """One CodexBrain per conversation session. Create one, call connect()
    once (a no-op — nothing to keep open between turns), then run_turn(text)/
    run_turn_stream(text) per incoming message, and disconnect() (also a
    no-op) on shutdown."""

    def __init__(self, vault_dir: str | None = None, cwd_override: str | None = None,
                 session_id: str | None = None, model: str | None = None, is_admin: bool = False,
                 project_id: str | None = None, effort: str | None = None):
        self.cwd = cwd_override or vault_dir or resolve_vault_dir()
        self.model = model
        # Reasoning effort for this session (David's ask 2026-09-15). Codex
        # has no `--effort` flag — see _build_args() for the config-override
        # route and why it's placed where it is. None sends nothing at all,
        # leaving the CLI's own configured default untouched, exactly as
        # before this option existed.
        self.effort = effort
        self.session_id = session_id
        self.is_admin = is_admin
        # Projects (David's ask 2026-09-12) — appended to the landing-zone
        # prompt below, same as core/brain.py/core/external_brain.py (see
        # core/projects.py's project_addendum()).
        self.project_id = project_id
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
        # Reasoning effort has no dedicated flag on `codex exec`; the
        # supported route is a config override. Verified live before relying
        # on it (codex-cli 0.154.0): `-c model_reasoning_effort="high"` is
        # accepted BEFORE the `resume` subcommand and exits 0, unlike -C/-s
        # which resume rejects outright — so unlike those two this can be
        # applied to resumed turns as well as fresh ones, and is inserted
        # directly after `exec` in both branches below for that reason.
        #
        # The value is TOML-parsed by codex, hence the embedded quotes. Two
        # things make that safe rather than an injection surface: this is
        # argv to create_subprocess_exec (no shell anywhere), and the value
        # itself is already constrained to one of the provider-advertised
        # effort strings by core/model_catalog.py's validate_effort() at the
        # route boundary. That validation is not belt-and-braces: `codex
        # exec --strict-config` checks unknown config KEYS but not their
        # values, confirmed live, so an unvalidated effort would pass
        # parsing and only surface later as a failed turn.
        effort_args = ["-c", f'model_reasoning_effort="{self.effort}"'] if self.effort else []
        if self.thread_id:
            # Resume doesn't take -C/-s — the original invocation already
            # fixed the working directory and sandbox mode for this thread.
            args = [codex, "exec", *effort_args, "resume", self.thread_id, "--json", "--skip-git-repo-check"]
        else:
            args = [codex, "exec", *effort_args, "--json", "--skip-git-repo-check", "-s", "workspace-write", "-C", self.cwd]
            # A generated file needs somewhere to be built that isn't the
            # vault (David's ask 2026-09-12 — see system_prompt.py's
            # _GENERATED_FILES_ADDENDUM), granted unconditionally: unlike
            # REPO_CODE_DIRS below this is just an output dropbox, not
            # source access, so it isn't admin-gated.
            os.makedirs(image_gen.GENERATED_FILES_DIR, exist_ok=True)
            args += ["--add-dir", image_gen.GENERATED_FILES_DIR]
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
            prompt_text = system_prompt.for_codex(sys.executable, HIVE_MIND_CLI_PATH, self.is_admin) + projects.project_addendum(self.project_id)
            prior = session_manager.effective_messages(self.session_id, exclude_last=True)
            if prior:
                transcript = "\n\n".join(f'{m["role"]}: {m["content"]}' for m in prior)
                prompt_text += f"\n\n[Earlier conversation, for context:]\n{transcript}\n[End of earlier conversation]"
            prompt = f"[System instructions:]\n{prompt_text}\n\n[User message:]\n{user_text}"
        else:
            prompt = user_text

        # search_sessions (hive_mind_cli.py) excludes this session's own
        # history the same way core/hive_mind_server.py's Claude tool does —
        # via an env var baked in here rather than trusting the model to
        # pass its own session id as an argument (it doesn't actually know
        # it). JARVIS_INTERNAL_TOKEN lets hive_mind_cli.py's write commands
        # (create/update/delete note/task/event) authenticate to this same
        # backend's own /api/* routes as "internal-tool" — see
        # mcp_servers/hive_mind_cli.py's docstring for why writes go over
        # HTTP rather than direct file access. Full os.environ is preserved;
        # only these two vars are added.
        env = {**os.environ, "JARVIS_CODEX_SESSION_ID": self.session_id or "", "JARVIS_INTERNAL_TOKEN": INTERNAL_TOOL_TOKEN}

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.write_eof()

        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            async for chunk in self._consume_process(proc, stderr_task, is_fresh_thread, user_text):
                yield chunk
        finally:
            await _kill_process_tree(proc)
            if not stderr_task.done():
                stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def _consume_process(self, proc, stderr_task, is_fresh_thread, user_text):
        has_text = False
        completed = False
        failure = None
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=CODEX_MESSAGE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RuntimeError("Codex timed out waiting for a response")
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
                if has_text:
                    yield "\n\n"
                has_text = True
                yield event["item"]["text"]
            elif etype == "turn.completed":
                completed = True
                usage = event.get("usage") or {}
                # input_tokens/output_tokens are the true, non-overlapping
                # totals; cached_input_tokens/reasoning_output_tokens are
                # subsets of those two (verified live), so summing every
                # *_tokens key the way core/token_usage.py's generic Claude
                # path does would double-count. Pre-summed here instead.
                #
                # input_tokens is kept alongside that pre-summed total
                # (David's ask 2026-09-15, the context indicator): it is the
                # size of the context this turn actually sent, which is a
                # different quantity from the cumulative spend total above
                # and cannot be recovered from it. Adding keys is safe for
                # the existing counter — core/token_usage.py's
                # _extract_total_tokens() prefers an explicit "total_tokens"
                # and returns early, so it never sees these and cannot
                # double-count them.
                self.last_usage = {
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    "input_tokens": usage.get("input_tokens", 0),
                    "cached_input_tokens": usage.get("cached_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                }
            elif etype in ("error", "turn.failed"):
                failure = event.get("message") or (event.get("error") or {}).get("message") or "Codex could not complete this turn"

        rc = await proc.wait()
        if rc != 0:
            # Self-heal any failed resume — not just the "no rollout found"
            # case (a persisted thread_id archived/deleted/pruned on the
            # codex side), but also a thread wedged by a lock a prior killed
            # process never released (see _kill_process_tree's docstring):
            # that failure mode's stderr text isn't guaranteed to be stable,
            # and a permanently-stuck session is strictly worse than losing
            # thread continuity once. Without this, every future turn in
            # this session would keep resuming the same broken id and fail
            # the same way forever. Only fires when this was actually a
            # resume attempt, so a genuinely fresh thread that fails for
            # some other reason isn't retried into a loop.
            if not is_fresh_thread:
                self.thread_id = None
                if self.session_id:
                    session_manager.set_codex_thread_id(self.session_id, None)
                async for chunk in self.run_turn_stream(user_text):
                    yield chunk
                return
            raise RuntimeError(f"Codex exited with an error (code {rc}). Check the selected model and CLI connection.")
        if failure or not completed:
            raise RuntimeError("Codex did not report a successful turn. Check the selected model and CLI connection.")

    async def disconnect(self) -> None:
        pass  # no persistent process to close
