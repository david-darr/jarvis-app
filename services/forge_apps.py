"""Session-owned development servers. No shell and no inherited app credentials."""
import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from core import permissions
from services.forge_sessions import forge_sessions, confined

ENV_KEYS = frozenset({'PATH', 'SYSTEMROOT', 'WINDIR', 'HOME', 'USERPROFILE',
                      'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP', 'TMPDIR', 'LANG'})
URL_PATTERN = re.compile(r'http://(?:localhost|127\.0\.0\.1):(\d{1,5})(?=[/\s\x1b]|$)', re.I)
CHOICES = [{'id': 'once', 'label': 'Approve command', 'behavior': 'allow', 'scope': 'once'},
           {'id': 'reject', 'label': 'Cancel', 'behavior': 'deny', 'scope': 'once'}]


def child_env(port=None):
    result = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEYS}
    result.update(LANG=os.environ.get('LANG', 'en_US.UTF-8'))
    if port is not None:
        result.update(PORT=str(port), BROWSER='none')
    return result


def command_args(command):
    if not isinstance(command, str) or not command.strip() or len(command) > 4000:
        raise ValueError('Enter an app command, up to 4,000 characters.')
    # Commands are portable argument lists. Quoted paths may contain backslashes.
    quote = None
    requires_shell = False
    for char in command:
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in '|&;<>`$':
            requires_shell = True
    if any(ord(c) < 32 for c in command) or requires_shell:
        raise ValueError('Shell syntax is not supported. Use a single executable and its arguments.')
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.escape = ''
    args = list(lexer)
    if not args or '=' in args[0] or Path(args[0]).stem.casefold() in {'cmd', 'powershell', 'pwsh', 'sh', 'bash', 'zsh', 'fish'}:
        raise ValueError('Commands requiring a shell cannot run in App Preview.')
    return args


def resolve_command(args, env, windows=None, cwd=None):
    """Resolve .cmd package shims to Node + their installed JS, never cmd.exe."""
    windows = os.name == 'nt' if windows is None else windows
    requested = args[0]
    if cwd and ('/' in requested or '\\' in requested) and not Path(requested).is_absolute():
        requested = str(Path(cwd) / requested)
    executable = shutil.which(requested, path=env.get('PATH', ''))
    if not executable:
        raise ValueError(f'Executable not found: {args[0]}')
    path = Path(executable).resolve()
    if windows and path.suffix.lower() in {'.cmd', '.bat', '.ps1'}:
        # Known package layouts only. Do not interpret a batch file's contents.
        entries = {'npm': 'npm/bin/npm-cli.js', 'npx': 'npm/bin/npx-cli.js',
                   'pnpm': 'pnpm/bin/pnpm.cjs', 'yarn': 'yarn/bin/yarn.js'}
        name = path.stem.lower()
        relative = entries.get(name)
        candidates = ([path.parent / 'node_modules' / relative,
                       path.parent / 'node_modules' / 'corepack' / 'dist' / (name + '.js')]
                      if relative else [])
        entry = next((p for p in candidates if p.is_file()), None)
        node = path.parent / 'node.exe'
        node = str(node) if node.is_file() else shutil.which('node.exe', path=env.get('PATH', ''))
        if not entry or not entry.is_file() or not node:
            raise ValueError('This command shim needs a shell. Use the executable or Node entry point directly.')
        return [str(Path(node).resolve()), str(entry.resolve()), *args[1:]]
    return [str(path), *args[1:]]


class WindowsJob:
    """Kill-on-close job owns descendants, even after the launcher exits."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64),
                        ('Flags', w.DWORD), ('Min', ctypes.c_size_t), ('Max', ctypes.c_size_t),
                        ('Limit', w.DWORD), ('Affinity', ctypes.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', ctypes.c_size_t),
                        ('JobMemory', ctypes.c_size_t), ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended(); limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign_and_resume(self, process):
        import ctypes
        from ctypes import wintypes as w
        if not self.kernel.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = [w.HANDLE]
        resume.restype = ctypes.c_long
        if resume(w.HANDLE(int(process._handle))) < 0:
            raise OSError('Could not resume the app process.')

    def assign_pid(self, pid):
        """Attach a PTY shell to the same kill-on-close ownership used by apps."""
        import ctypes
        from ctypes import wintypes as w
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        handle = self.kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        try:
            if not handle or not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if handle:
                self.kernel.CloseHandle(handle)

    def terminate_and_wait(self, timeout=5.0):
        """Kill every process in the job and wait until none is left.

        Closing a kill-on-close job ends its processes asynchronously, so a
        descendant could still hold the worktree folder after the launcher
        exited (found 2026-10-09: a venv python.exe is a launcher whose real
        interpreter is a child). Returns True when the job emptied in time.
        """
        import ctypes
        import time
        from ctypes import wintypes as w
        if not getattr(self, 'handle', None):
            return True
        class Accounting(ctypes.Structure):
            _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                        ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                        ('TotalPageFaultCount', w.DWORD), ('TotalProcesses', w.DWORD),
                        ('ActiveProcesses', w.DWORD), ('TotalTerminatedProcesses', w.DWORD)]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.kernel.TerminateJobObject(self.handle, 1)
        info = Accounting()
        deadline = time.monotonic() + timeout
        while True:
            # 1 = JobObjectBasicAccountingInformation
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return False
            if info.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def close(self):
        if getattr(self, 'handle', None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ForgeApps:
    def __init__(self, sessions=None, backend_port=None, timeout=30):
        self.sessions = sessions or forge_sessions
        self.projects = self.sessions.projects
        self.backend_port = backend_port
        self.timeout = timeout
        self.lock = threading.RLock()
        self.records = {}
        self.epochs = {}
        self.start_locks = {}
        self.closed = False

    def forbidden_port(self):
        from core.middleware import local_api_base
        return self.backend_port or urlsplit(local_api_base()).port

    def suggest(self, session_id):
        root = self.sessions.workspace(session_id)
        project = self.projects.get(self.sessions.get(session_id)['forge']['project_id'])
        suggestions = []
        def exists(name):
            return confined(root, name).is_file()
        runner = next((name for name, locks in [('bun', ('bun.lock', 'bun.lockb')), ('pnpm', ('pnpm-lock.yaml',)),
                      ('yarn', ('yarn.lock',)), ('npm', ('package-lock.json', 'npm-shrinkwrap.json'))]
                       if any(exists(lock) for lock in locks)), 'npm')
        package = {}
        if exists('package.json'):
            package = json.loads(self.sessions._text(confined(root, 'package.json')))
            if not isinstance(package, dict) or not isinstance(package.get('scripts', {}), dict):
                raise ValueError('package.json must contain an object with a scripts object.')
            scripts = package.get('scripts') or {}
            for name in ('dev', 'start', 'preview'):
                if isinstance(scripts.get(name), str):
                    command = f'{runner} run {name}'
                    if re.search(r'\bvite\b', scripts[name]):
                        command += (' --' if runner == 'npm' else '') + ' --host 127.0.0.1 --port {port}'
                    elif re.search(r'\bnext\b', scripts[name]):
                        command += (' --' if runner == 'npm' else '') + ' --hostname 127.0.0.1 --port {port}'
                    suggestions.append(command)
            deps = {}
            for section in ('dependencies', 'devDependencies'):
                if isinstance(package.get(section), dict):
                    deps.update(package[section])
            if not suggestions:
                for name, flags in [('vite', '--host 127.0.0.1 --port {port}'), ('next', 'dev --hostname 127.0.0.1 --port {port}')]:
                    if name in deps:
                        # Execute the installed entry point, without npx downloading anything.
                        suggestions.append(f'node node_modules/{name}/' + ('bin/vite.js ' if name == 'vite' else 'dist/bin/next ') + flags)
        if exists('manage.py'):
            suggestions.append('python manage.py runserver 127.0.0.1:{port} --noreload')
        if exists('app.py') or exists('wsgi.py'):
            text = self.sessions._text(confined(root, 'app.py' if exists('app.py') else 'wsgi.py'))
            if re.search(r'\bflask\b', text, re.I):
                suggestions.append('python -m flask --app ' + ('app' if exists('app.py') else 'wsgi') + ' run --host 127.0.0.1 --port {port}')
        if exists('bin/rails'):
            suggestions.append('ruby bin/rails server -b 127.0.0.1 -p {port}')
        return dict(command=project.get('app_command', ''), suggestions=suggestions)

    def set_command(self, project_id, command):
        command_args(command)
        return self.projects.set_app_command(project_id, command.strip())

    def _free_port(self):
        for _ in range(20):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            if port != self.forbidden_port() and all(port not in r['ports'] for r in self.records.values()):
                return port
        raise ValueError('Could not reserve an app port.')

    async def start(self, session_id, is_admin=False, surface=None):
        if not is_admin:
            raise ValueError('App Preview requires an admin.')
        async with self.start_locks.setdefault(session_id, asyncio.Lock()):
            with self.lock:
                if self.closed:
                    raise ValueError('Kairos is shutting down.')
                root = self.sessions.workspace(session_id)
                project_id = self.sessions.get(session_id)['forge']['project_id']
                project = self.projects.get(project_id)
                command = project.get('app_command', '')
                parsed = command_args(command)
                running = self.records.get(session_id)
                if running and running['process'].poll() is None:
                    return self.status(session_id)
                self._stop(session_id)
                epoch = self.epochs.get(session_id, 0)
                port = self._free_port()
                env = child_env(port)
                args = resolve_command([a.replace('{port}', str(port)) for a in parsed], env, cwd=root)
                exact = command.replace('{port}', str(port))
                digest = hashlib.sha256(command.encode()).hexdigest()
            if project.get('app_command_hash') != digest:
                decision = await permissions.decide(surface=surface or f'forge-app:{session_id}',
                    tool='forge_app_start', arguments={'command': exact, 'folder': str(root), 'argv': args},
                    title='Run app?', description=f'Command: {exact}\nFolder: {root}', target=exact,
                    is_admin=True, force_prompt=True, choices=CHOICES)
                if decision.behavior != 'allow':
                    raise ValueError(decision.reason or 'App command was not approved.')
            with self.lock:
                if self.closed or self.epochs.get(session_id, 0) != epoch:
                    raise ValueError('App start was cancelled because the session stopped.')
                with self.sessions.lock:
                    if self.sessions.workspace(session_id) != root or self.projects.get(project_id).get('app_command') != command:
                        raise ValueError('The command or folder changed during approval. Try again.')
                    self.projects.approve_app_command(project_id, command, digest)
                    job = WindowsJob() if os.name == 'nt' else None
                    process = None
                    try:
                        process = subprocess.Popen(args, cwd=str(root), env=env, shell=False,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=0, start_new_session=os.name != 'nt',
                            creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0) | 4) if job else 0)
                        if job:
                            job.assign_and_resume(process)
                    except BaseException:
                        if job:
                            job.close()
                        if process:
                            process.kill(); process.wait(); process.stdout.close()
                        raise
                    record = dict(session_id=session_id, project_id=project_id, command=exact, folder=str(root),
                                  process=process, job=job, port=port, ports={port}, ready=False,
                                  lines=deque(maxlen=2000), started_at=time.time())
                    self.records[session_id] = record
                    reader = threading.Thread(target=self._read, args=(record,), daemon=True)
                    record['reader'] = reader; reader.start()
            try:
                await asyncio.to_thread(self._wait_ready, record)
            except BaseException:
                with self.lock:
                    if self.records.get(session_id) is record:
                        self._stop(session_id)
                raise
            return self.status(session_id)

    def _line(self, record, line):
        with self.lock:
            record['lines'].append(line[:8192])
            for found in URL_PATTERN.finditer(line):
                port = int(found[1])
                if 0 < port < 65536 and port != self.forbidden_port():
                    record['ports'].add(port)

    def _read(self, record):
        pipe = record['process'].stdout
        pending = b''
        try:
            while chunk := pipe.read(4096):
                pending += chunk
                while b'\n' in pending or len(pending) >= 8192:
                    at = pending.find(b'\n')
                    size = min(at + 1 if at >= 0 else 8192, 8192)
                    self._line(record, pending[:size].decode('utf-8', errors='replace').rstrip('\r\n'))
                    pending = pending[size:]
            if pending:
                self._line(record, pending.decode('utf-8', errors='replace'))
        finally:
            pipe.close()
            # A failed launcher must not leave its children serving indefinitely.
            # Only for a record not already stopped: _stop moves the epoch, and a
            # late reader on a stopped app would cancel a Restart's new start.
            with self.lock:
                if self.records.get(record['session_id']) is record and not record.get('terminated'):
                    self._stop(record['session_id'])

    def _wait_ready(self, record):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.records.get(record['session_id']) is not record or record['process'].poll() is not None:
                    raise ValueError('App exited before it was ready. Check its logs.')
                ports = sorted(record['ports'])
            for port in ports:
                if port == self.forbidden_port():
                    continue
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        with self.lock:
                            if record.get('terminated') or self.records.get(record['session_id']) is not record:
                                raise ValueError('App start was cancelled.')
                            record['port'] = port; record['ready'] = True
                        return
                except OSError:
                    pass
            time.sleep(.05)
        raise ValueError('App did not become ready in time. Check its logs and port settings.')

    def _stop(self, session_id):
        self.epochs[session_id] = self.epochs.get(session_id, 0) + 1
        record = self.records.get(session_id)
        if not record:
            return
        process = record['process']
        record['ready'] = False; record['ports'].clear()
        if record.get('terminated'):
            return
        record['terminated'] = True
        if record.get('job'):
            # Wait for descendants too, so the worktree is free when stop returns.
            record['job'].terminate_and_wait()
            record['job'].close()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Kill descendants even when the parent exited on SIGTERM.
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)

    def stop(self, session_id):
        self.sessions.get(session_id)
        with self.lock:
            self._stop(session_id)
        return self.status(session_id)

    async def restart(self, session_id, is_admin=False, surface=None):
        if not is_admin:
            raise ValueError('App Preview requires an admin.')
        self.stop(session_id)
        return await self.start(session_id, is_admin, surface)

    def status(self, session_id):
        self.sessions.get(session_id)
        with self.lock:
            record = self.records.get(session_id)
            if not record:
                return dict(session_id=session_id, running=False, ready=False, ports=[])
            running = record['process'].poll() is None and bool(record['ports'])
            return dict(session_id=session_id, project_id=record['project_id'], command=record['command'],
                        running=running, ready=running and record['ready'], port=record['port'],
                        ports=sorted(record['ports']) if running else [],
                        url=f"http://127.0.0.1:{record['port']}/" if running else None,
                        exit_code=record['process'].poll(), started_at=record['started_at'])

    def logs(self, session_id, limit=200):
        self.sessions.get(session_id)
        with self.lock:
            record = self.records.get(session_id)
            return dict(session_id=session_id, lines=list(record['lines'])[-max(1, min(2000, limit)):] if record else [])

    def running(self):
        with self.lock:
            return [status for sid in self.records if (status := self.status(sid))['running']]

    def shutdown(self):
        with self.lock:
            self.closed = True
            for sid in list(self.records):
                self._stop(sid)


forge_apps = ForgeApps()
