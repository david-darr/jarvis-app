"""Person-owned PTYs. SSE replays bounded output; agents have no terminal tool."""
import codecs
import os
import signal
import threading
import uuid
from collections import deque

from services.forge_sessions import forge_sessions
from services.forge_apps import child_env, resolve_command, WindowsJob

BUFFER_CAP = 200 * 1024
MAX_TERMINALS = 4


def shell_args(env):
    if os.name == 'nt':
        # No startup profile before assigning the shell to its process job.
        return resolve_command(['powershell.exe', '-NoLogo', '-NoProfile'], env)
    import pwd
    shell = pwd.getpwuid(os.getuid()).pw_shell or '/bin/sh'
    return resolve_command([shell, '-l'], env)


class OutputBuffer:
    def __init__(self, cap=BUFFER_CAP):
        self.cap = cap
        self.events = deque()
        self.size = 0
        self.sequence = 0

    def append(self, text):
        # Bound each event too, even if a PTY implementation returns a large read.
        body = text.encode('utf-8')[-self.cap:]
        text = body.decode('utf-8', errors='ignore')
        size = len(text.encode('utf-8'))
        self.sequence += 1
        self.events.append((self.sequence, text, size)); self.size += size
        while self.size > self.cap:
            self.size -= self.events.popleft()[2]

    def since(self, after=0):
        reset = bool(self.events and after < self.events[0][0] - 1)
        return dict(events=[{'id': seq, 'data': text} for seq, text, _ in self.events if seq > after],
                    reset=reset, sequence=self.sequence)


class ForgeTerminals:
    def __init__(self, sessions=None, spawn=None):
        self.sessions = sessions or forge_sessions
        self.spawn = spawn
        self.lock = threading.RLock()
        self.records = {}
        self.closed = False

    def start(self, session_id, rows=24, cols=80, is_admin=False, remote=False):
        if not is_admin:
            raise ValueError('The terminal requires an admin.')
        self.dimensions(rows, cols)
        with self.lock, self.sessions.lock:
            if self.closed:
                raise ValueError('Kairos is shutting down.')
            root = self.sessions.workspace(session_id)
            old = [r for r in self.records.values() if r['session_id'] == session_id]
            for record in old:
                if record['ended']:
                    self._close(record)
                    self.records.pop(record['id'], None)
            if sum(r['session_id'] == session_id for r in self.records.values()) >= MAX_TERMINALS:
                raise ValueError('You can open up to four terminals in a session. Close one first.')
            env = child_env(); env.update(TERM='xterm-256color', COLORTERM='truecolor')
            args = shell_args(env)
            if self.spawn:
                spawn = self.spawn
            elif os.name == 'nt':
                from winpty import PtyProcess
                spawn = PtyProcess.spawn
            else:
                from ptyprocess import PtyProcessUnicode
                spawn = PtyProcessUnicode.spawn
            job = WindowsJob() if os.name == 'nt' and not self.spawn else None
            process = None
            try:
                process = spawn(args, cwd=str(root), env=env, dimensions=(rows, cols))
                if job:
                    job.assign_pid(process.pid)
            except BaseException:
                if job: job.close()
                if process: process.close(force=True)
                raise
            terminal_id = uuid.uuid4().hex
            record = dict(id=terminal_id, session_id=session_id, process=process, job=job,
                          buffer=OutputBuffer(), ended=False, closing=False, remote=remote, io_lock=threading.RLock())
            self.records[terminal_id] = record
            reader = threading.Thread(target=self._read, args=(record,), daemon=True)
            record['reader'] = reader; reader.start()
            return dict(id=terminal_id, session_id=session_id)

    @staticmethod
    def dimensions(rows, cols):
        if type(rows) is not int or type(cols) is not int or not 2 <= rows <= 300 or not 2 <= cols <= 500:
            raise ValueError('Use between 2 and 300 rows, and 2 and 500 columns.')

    def get(self, session_id, terminal_id):
        self.sessions.get(session_id)
        record = self.records.get(terminal_id)
        if not record or record['session_id'] != session_id:
            raise KeyError('Terminal not found for this session.')
        return record

    def _read(self, record):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while not record['closing']:
                data = record['process'].read(4096)
                if not data: break
                if isinstance(data, bytes): data = decoder.decode(data)
                with self.lock:
                    record['buffer'].append(data)
        except (EOFError, OSError):
            pass
        finally:
            with self.lock:
                tail = decoder.decode(b'', final=True)
                if tail: record['buffer'].append(tail)
                record['ended'] = True
                # Close descendants when the shell itself exits too.
                self._close(record)

    def output(self, session_id, terminal_id, after=0):
        with self.lock:
            record = self.get(session_id, terminal_id)
            return {**record['buffer'].since(after), 'ended': record['ended'] or record['closing']}

    def write(self, session_id, terminal_id, data):
        if not isinstance(data, str) or len(data.encode('utf-8')) > 64 * 1024:
            raise ValueError('Terminal input exceeds the size limit.')
        with self.lock:
            record = self.get(session_id, terminal_id)
            with record['io_lock']:
                if record['ended'] or record['closing']: raise ValueError('This terminal has closed.')
                record['process'].write(data)
        return {'ok': True}

    def resize(self, session_id, terminal_id, rows, cols):
        self.dimensions(rows, cols)
        with self.lock:
            record = self.get(session_id, terminal_id)
            with record['io_lock']:
                if record['ended'] or record['closing']: raise ValueError('This terminal has closed.')
                record['process'].setwinsize(rows, cols)
        return {'ok': True}

    def _close(self, record):
        if record['closing']: return
        record['closing'] = True
        with record['io_lock']:
            if record['job']:
                record['job'].close()
            elif os.name != 'nt' and not self.spawn:
                try: os.killpg(record['process'].pid, signal.SIGKILL)
                except ProcessLookupError: pass
            record['process'].close(force=True)

    def close(self, session_id, terminal_id):
        with self.lock:
            record = self.get(session_id, terminal_id)
            self._close(record)
            self.records.pop(terminal_id, None)
        return {'ok': True}

    def stop_session(self, session_id):
        with self.lock:
            for terminal_id, record in list(self.records.items()):
                if record['session_id'] == session_id:
                    self._close(record)
                    self.records.pop(terminal_id, None)

    def shutdown(self):
        with self.lock:
            self.closed = True
            for record in list(self.records.values()): self._close(record)
            self.records.clear()

    def stop_remote(self):
        with self.lock:
            for terminal_id, record in list(self.records.items()):
                if record['remote']:
                    self._close(record)
                    self.records.pop(terminal_id, None)


forge_terminals = ForgeTerminals()
