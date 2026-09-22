"""Codex login transport with an authenticated, attempt-scoped MCP bridge.

The bridge is revoked before cancellation. Windows Job Objects and POSIX
process groups own the CLI and MCP helpers, including after a parent exits.
"""
import asyncio
import ctypes
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from . import LEAD_MAX_TURNS, MAX_TURNS, role_prompt, task_prompt
from ..models import EventKind, WorkerEvent
from ..tools import ToolRejected
from ..worker_bridge import WorkerBridge

MESSAGE_TIMEOUT_SECONDS = 180
STEP_TIMEOUT_SECONDS = 300
STOP_GRACE_SECONDS = 5
HELPER = Path(__file__).resolve().parents[1] / "codex_mcp.py"
SUPERVISOR_PYTHON = getattr(sys, "_base_executable", sys.executable)
VERIFIED_CLI_VERSION = "codex-cli 0.154.0"


def codex_path():
    path = shutil.which("codex")
    if not path:
        return None
    if sys.platform != "win32" or Path(path).suffix.lower() == ".exe":
        return path
    # npm's .cmd shim requires a shell on Windows. Use its packaged binary.
    package = Path(path).parent / "node_modules" / "@openai" / "codex"
    candidates = list(package.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
    candidates += list(package.glob("vendor/*/codex/codex.exe"))
    return str(candidates[0]) if len(candidates) == 1 else None


@lru_cache(maxsize=4)
def _installation_check(path, stamp):
    try:
        version = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=5, creationflags=0x08000000 if sys.platform == "win32" else 0)
        # New releases may introduce native tools enabled by default. Re-run
        # the installed-binary surface test before admitting another version.
        if version.returncode or version.stdout.strip() != VERIFIED_CLI_VERSION:
            return "This Codex CLI version has not been verified for Swarm's restricted tool surface (verified: 0.154.0)."
        result = subprocess.run([path, "exec", "--help"], capture_output=True, text=True,
                                timeout=5, creationflags=0x08000000 if sys.platform == "win32" else 0)
        required = ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config")
        if result.returncode or any(flag not in result.stdout for flag in required):
            return "Codex CLI lacks the isolated worker options; update the CLI before using Swarm."
    except (OSError, subprocess.SubprocessError):
        return "Codex CLI could not be inspected. Check its installation."
    return None


def installation_blocker():
    path = codex_path()
    if not path:
        return "Codex CLI executable not found. Install Codex CLI and sign in before using Swarm."
    try:
        return _installation_check(path, Path(path).stat().st_mtime_ns)
    except OSError:
        return "Codex CLI executable is unavailable. Check its installation."


def worker_environment(extra=None):
    # Login stays in CODEX_HOME/the OS credential store. No app/API credentials.
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
               "CODEX_HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {key: value for key, value in {**os.environ, **(extra or {})}.items()
            if key.upper() in allowed}


def config_args(values):
    result = []
    for key, value in values.items():
        result += ["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"]
    return result


def model_record(model):
    """Public CLI model metadata only; authentication files are never read."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = home / "models_cache.json"
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return None
        models = json.loads(path.read_text(encoding="utf-8")).get("models", [])
        return next((row for row in models if row.get("slug") == model and row.get("visibility") != "hide"), None)
    except (OSError, ValueError, AttributeError):
        return None


class _WindowsJob:
    """Kill-on-close, non-inheritable ownership of the entire worker tree."""
    def __init__(self):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
            _fields_ += [(name, w.DWORD) for name in
                         ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

        class ProcessIds(ctypes.Structure):
            _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("ids", ctypes.c_size_t * 4096)]

        self.accounting = Accounting
        self.process_ids = ProcessIds
        self.process_handles = []
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Could not create worker process job")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("Could not configure worker process job")

    def attach(self, pid):
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if not process or not self.api.AssignProcessToJobObject(self.handle, process):
                raise OSError("Could not contain Codex process tree")
        finally:
            if process:
                self.api.CloseHandle(process)

    def active(self):
        info = self.accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError("Could not verify Codex process tree")
        return info.ActiveProcesses

    def terminate(self):
        info = self.process_ids()
        if not self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError("Could not enumerate the worker job before stopping")
        for pid in info.ids[:info.count]:
            handle = self.api.OpenProcess(0x100000, False, pid)
            if handle:
                self.process_handles.append(handle)
        if not self.api.TerminateJobObject(self.handle, 1):
            raise OSError("Could not terminate Codex process tree")

    def exited(self):
        # Job accounting can reach zero before process handles signal exit.
        # Wait for both before releasing scratch files or confirming a stop.
        return self.active() == 0 and all(self.api.WaitForSingleObject(handle, 0) == 0
                                         for handle in self.process_handles)

    def close(self):
        for handle in self.process_handles:
            self.api.CloseHandle(handle)
        self.process_handles.clear()
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class CodexWorker:
    def __init__(self, context):
        self.context = context
        self.process = None
        self.job = None
        self.job_attached = False
        self.bridge = None
        self.stopped = None
        self.cancelled = False
        self.stop_lock = asyncio.Lock()
        self.stderr = ""

    def _args(self, codex, scratch, assignment):
        model = self.context.model or (self.context.endpoint or {}).get("model")
        record = model_record(model)
        if record is None:
            raise RuntimeError("Choose a model from the Codex CLI catalog before starting this worker.")
        # Native file editing is selected by model metadata, independently of
        # shell_tool. Preserve the provider's model properties while removing
        # that optional tool from this invocation's private catalog.
        catalog = Path(scratch) / "worker-model.json"
        catalog.write_text(json.dumps({"models": [{**record, "apply_patch_tool_type": None}]}), encoding="utf-8")
        values = {
            "model_catalog_json": str(catalog),
            "approval_policy": "never", "web_search": "disabled",
            "project_doc_max_bytes": 0, "project_root_markers": [".swarm-root"],
            "features.shell_tool": False, "features.unified_exec": False,
            "features.shell_snapshot": False, "features.apps": False,
            "features.sleep_tool": False,
            "features.plugins": False, "features.remote_plugin": False,
            "features.hooks": False, "features.multi_agent": False,
            "agents.enabled": False,
            "features.multi_agent_v2": False, "features.code_mode": False,
            "features.code_mode_host": True, "features.browser_use": False,
            "features.computer_use": False, "features.image_generation": False,
            "features.view_image": False,
            "features.memories": False, "features.skill_search": False,
            "features.skip_host_skill_discovery": True,
            "features.skill_mcp_dependency_install": False,
            "features.tool_suggest": False, "features.goals": False,
            "features.rollout_budget.enabled": True,
            "features.rollout_budget.limit_tokens": assignment.max_units,
            "features.rollout_budget.reminder_at_remaining_tokens": [max(1, assignment.max_units // 5)],
            "mcp_servers.swarm.command": sys.executable,
            "mcp_servers.swarm.args": ["-I", str(HELPER)],
            "mcp_servers.swarm.env_vars": ["JARVIS_SWARM_BRIDGE_PORT", "JARVIS_SWARM_BRIDGE_TOKEN"],
            "mcp_servers.swarm.required": True,
            "mcp_servers.swarm.enabled": True,
            "mcp_servers.swarm.default_tools_approval_mode": "approve",
            "mcp_servers.swarm.enabled_tools": list(self.context.tool_service.definitions),
            "mcp_servers.swarm.startup_timeout_sec": 15,
            "mcp_servers.swarm.tool_timeout_sec": 65,
        }
        if self.context.effort:
            values["model_reasoning_effort"] = self.context.effort
        args = [codex, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--strict-config", "--skip-git-repo-check", "-s", "read-only", "-C", scratch,
                *config_args(values)]
        if model:
            args += ["-m", model]
        return [*args, "-"]

    async def _stderr(self):
        while True:
            data = await self.process.stderr.read(4096)
            if not data:
                return
            self.stderr = (self.stderr + data.decode("utf-8", errors="replace"))[-8000:]

    async def events(self, assignment):
        service = self.context.tool_service.bind(assignment)
        blocker = installation_blocker()
        if blocker:
            raise RuntimeError(blocker)
        queue = asyncio.Queue()
        scratch = tempfile.TemporaryDirectory(prefix="attempt-", dir=self.context.scratch_dir)
        Path(scratch.name, ".swarm-root").touch()
        self.bridge = WorkerBridge(service, queue)
        tasks = []
        usage_reported = False
        completed = False
        failure = None
        calls = 0
        bound = LEAD_MAX_TURNS if self.context.is_lead else MAX_TURNS
        incomplete = None
        try:
            env = worker_environment(self.context.env)
            # Cancellation owns this same lock: it cannot declare a stop while
            # an OS spawn is still producing a process we have not recorded.
            async with self.stop_lock:
                if self.cancelled:
                    return
                await self.bridge.start()
                env.update(JARVIS_SWARM_BRIDGE_PORT=str(self.bridge.port), JARVIS_SWARM_BRIDGE_TOKEN=self.bridge.token)
                if sys.platform == "win32":
                    self.job = _WindowsJob()
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    # A Windows venv python.exe is a launcher: it may spawn
                    # the real interpreter before we can attach its job.
                    # Start the base interpreter directly for this stdlib helper.
                    SUPERVISOR_PYTHON, "-I", str(HELPER), "--supervise",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=env, limit=1024 * 1024,
                    **({"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True})))
                try:
                    self.process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    self.process = await spawning
                    raise
                if self.job:
                    self.job.attach(self.process.pid)
                    self.job_attached = True
                if self.cancelled:
                    return
                prompt = f"{role_prompt(self.context)}\n\n{task_prompt(assignment.objective)}"
                launch = {"argv": self._args(codex_path(), scratch.name, assignment), "cwd": scratch.name, "prompt": prompt}
                self.process.stdin.write(json.dumps(launch).encode() + b"\n")
                await self.process.stdin.drain()
                self.process.stdin.close()
            stderr_task = asyncio.create_task(self._stderr())
            reading = asyncio.create_task(self.process.stdout.readline())
            action = asyncio.create_task(queue.get())
            tasks = [stderr_task, reading, action]
            deadline = time.monotonic() + STEP_TIMEOUT_SECONDS
            while not self.cancelled:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    incomplete = "The Codex step reached its time limit."
                    break
                done, _ = await asyncio.wait([reading, action], timeout=min(remaining, MESSAGE_TIMEOUT_SECONDS),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    incomplete = "The Codex step timed out waiting for progress."
                    break
                if action in done:
                    key, name, arguments, future = action.result()
                    action = asyncio.create_task(queue.get())
                    tasks[2] = action
                    if calls >= bound:
                        future.set_result({"ok": False, "text": "This step reached its tool-call limit."})
                        incomplete = f"The Codex step reached its {bound}-tool-call limit."
                        break
                    calls += 1
                    yield WorkerEvent(EventKind.ACTION_STARTED, f"{key}:start",
                                      {"action_id": key, "intent": {"tool": name, "arguments": arguments}})
                    # No await between live-state check and the domain operation.
                    self.bridge.validate()
                    try:
                        answer = {"ok": True, "text": service.call(name, arguments)}
                    except ToolRejected as rejected:
                        answer = {"ok": False, "text": f"Rejected: {rejected}"}
                    yield WorkerEvent(EventKind.ACTION_FINISHED, f"{key}:end", {"action_id": key, "result": answer})
                    if not future.done():
                        future.set_result(answer)
                if reading in done:
                    line = reading.result()
                    if not line:
                        break
                    reading = asyncio.create_task(self.process.stdout.readline())
                    tasks[1] = reading
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    kind, item = event.get("type"), event.get("item") or {}
                    if kind == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                        yield WorkerEvent(EventKind.TEXT, uuid.uuid4().hex, {"text": item["text"]})
                    elif kind == "turn.completed":
                        completed = True
                        usage = event.get("usage") or {}
                        if all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ("input_tokens", "output_tokens")):
                            if usage_reported:
                                raise RuntimeError("Codex repeated a final usage report")
                            usage_reported = True
                            yield WorkerEvent(EventKind.USAGE, f"{assignment.attempt_id}:turn",
                                              {"units": usage["input_tokens"] + usage["output_tokens"]})
                    elif kind in ("error", "turn.failed"):
                        failure = event.get("message") or (event.get("error") or {}).get("message") or "Codex turn failed"
            if self.cancelled:
                return
            if incomplete:
                await self._terminate()
            else:
                code = await asyncio.wait_for(self.process.wait(), STOP_GRACE_SECONDS)
                await asyncio.wait_for(stderr_task, STOP_GRACE_SECONDS)
                if code or failure or not completed:
                    detail = str(failure or self.stderr.strip()[-1500:] or "No successful turn was reported")
                    detail = detail.replace(self.bridge.token, "[redacted]")
                    raise RuntimeError(f"Codex ended unsuccessfully (exit {code}): {detail}")
            yield WorkerEvent(EventKind.RESULT, f"{assignment.attempt_id}:final", {
                "result": service.terminal or {"status": "incomplete", "reason": incomplete or "Codex ended without submitting work."},
                "usage_complete": usage_reported,
            })
        finally:
            await self._terminate()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.stopped:
                scratch.cleanup()
            else:
                scratch._finalizer.detach()

    async def _terminate(self):
        async with self.stop_lock:
            if self.bridge:
                await self.bridge.close()
            if self.stopped is not None:
                return
            process = self.process
            if process is None:
                self.stopped = True
                if self.job:
                    self.job.close()
                return
            confirmed = False
            try:
                if self.job and self.job_attached:
                    self.job.terminate()
                    until = time.monotonic() + STOP_GRACE_SECONDS
                    while not self.job.exited() and time.monotonic() < until:
                        await asyncio.sleep(0.02)
                    confirmed = self.job.exited()
                elif sys.platform != "win32":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        confirmed = True
                else:
                    process.kill()  # supervisor has not received its launch line
                    await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
                    confirmed = True
                await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
            except (OSError, asyncio.TimeoutError):
                confirmed = False
            finally:
                if self.job:
                    self.job.close()
            self.stopped = confirmed

    async def cancel(self):
        self.cancelled = True
        await self._terminate()
        return self.stopped is True
