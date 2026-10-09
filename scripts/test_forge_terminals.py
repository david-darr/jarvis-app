"""PTY contracts with an in-memory transport. --integration starts the OS PTY."""
import os
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['JARVIS_DATA_DIR'] = str(ROOT / 'data' / 'forge3-unit-tests')
from services.forge_terminals import ForgeTerminals, OutputBuffer, BUFFER_CAP
from services.forge_apps import child_env
from services.forge_terminal_access import local_terminal_request
from core.settings import DEFAULTS


class FakePty:
    def __init__(self, *_args, **kwargs):
        self.env = kwargs['env']; self.cwd = kwargs['cwd']; self.dimensions = kwargs['dimensions']
        self.queue = queue.Queue(); self.closed = False
    def read(self, _size):
        value = self.queue.get()
        if value is None: raise EOFError()
        return value
    def write(self, data): self.queue.put(data)
    def setwinsize(self, rows, cols): self.dimensions = rows, cols
    def close(self, force=False):
        self.closed = True; self.queue.put(None)


class TerminalTests(unittest.TestCase):
    def service(self):
        sessions = Mock(); sessions.lock = threading.RLock(); sessions.workspace.return_value = ROOT
        service = ForgeTerminals(sessions, FakePty)
        self.addCleanup(service.shutdown)
        return service

    def test_ring_cap_unicode_replay_and_cursor(self):
        ring = OutputBuffer(32)
        for _ in range(30): ring.append('🛠 hello\n')
        self.assertLessEqual(ring.size, 32)
        self.assertTrue(ring.since(0)['reset'])
        last = ring.since(0)['events'][-1]['id']
        self.assertEqual(ring.since(last)['events'], [])
        ring.append('é' * 100)
        self.assertLessEqual(ring.size, 32)
        self.assertEqual(BUFFER_CAP, 200 * 1024)

    def test_environment_no_tokens_and_remote_default(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'JARVIS_UI_SECRET': 'secret', 'ANTHROPIC_API_KEY': 'secret', 'PATH': 'path'}):
            env = child_env()
            self.assertEqual(env['PATH'], 'path')
            for key in ['OPENAI_API_KEY', 'JARVIS_UI_SECRET', 'ANTHROPIC_API_KEY', 'PORT']: self.assertNotIn(key, env)
        self.assertFalse(DEFAULTS['forge_terminal_remote'])

    def test_remote_listener_and_peer_are_not_host_headers(self):
        local = dict(scheme='http', server=('127.0.0.1', 8420), client=('127.0.0.1', 50000))
        self.assertTrue(local_terminal_request(local))
        for changed in [{'scheme': 'https'}, {'server': ('100.90.1.1', 8422)}, {'client': ('100.90.2.2', 8000)}, {'server': None}, {'client': None}]:
            self.assertFalse(local_terminal_request({**local, **changed}))
        self.assertTrue(local_terminal_request({**local, 'server': ('::1', 8420), 'client': ('::ffff:127.0.0.1', 10)}))

    def test_start_write_read_resize_close_session_and_cap(self):
        service = self.service()
        with patch('services.forge_terminals.shell_args', return_value=['fixture-shell']):
            with self.assertRaisesRegex(ValueError, 'admin'): service.start('s')
            ids = [service.start('s', is_admin=True)['id'] for _ in range(4)]
            with self.assertRaisesRegex(ValueError, 'four'): service.start('s', is_admin=True)
        service.write('s', ids[0], 'echo fixture\r\n')
        for _ in range(100):
            output = service.output('s', ids[0])
            if output['events']: break
            time.sleep(.001)
        self.assertEqual(output['events'][0]['data'], 'echo fixture\r\n')
        service.resize('s', ids[0], 30, 100)
        self.assertEqual(service.records[ids[0]]['process'].dimensions, (30, 100))
        self.assertEqual(service.records[ids[0]]['process'].cwd, str(ROOT))
        with self.assertRaises(KeyError): service.write('other-session', ids[0], 'bad')
        processes = [service.records[id]['process'] for id in ids]
        service.close('s', ids[0]); self.assertTrue(processes[0].closed)
        service.stop_session('s'); self.assertTrue(all(p.closed for p in processes))
        self.assertFalse(service.records)

    def test_shutdown_and_size_limits(self):
        service = self.service()
        with patch('services.forge_terminals.shell_args', return_value=['fixture-shell']):
            id = service.start('s', is_admin=True)['id']
            process = service.records[id]['process']
            with self.assertRaises(ValueError): service.write('s', id, 'é' * 65536)
            for rows, cols in [(0, 80), (24, 10000), (True, 80)]:
                with self.assertRaises(ValueError): service.resize('s', id, rows, cols)
            service.shutdown(); self.assertTrue(process.closed)
            with self.assertRaisesRegex(ValueError, 'shutting down'): service.start('s', is_admin=True)

    def test_disabling_remote_access_closes_remote_shells(self):
        service = self.service()
        with patch('services.forge_terminals.shell_args', return_value=['fixture-shell']):
            local = service.start('s', is_admin=True)['id']
            remote = service.start('s', is_admin=True, remote=True)['id']
        process = service.records[remote]['process']
        service.stop_remote()
        self.assertTrue(process.closed)
        self.assertIn(local, service.records)
        self.assertNotIn(remote, service.records)

    def test_routes_all_admin_remote_gate_and_cleanup_hooks_no_agent_tool(self):
        source = (ROOT / 'routes/forge_routes.py').read_text(encoding='utf-8')
        terminal_routes = [line for line in source.splitlines() if line.startswith('@router.') and '/terminals' in line]
        self.assertEqual(len(terminal_routes), 5)
        self.assertTrue(all('dependencies=[Depends(terminal_access)]' in line for line in terminal_routes))
        self.assertIn('user: str = Depends(require_admin)', source)
        self.assertIn('forge_terminals.shutdown()', (ROOT / 'app.py').read_text(encoding='utf-8'))
        self.assertIn('forge_terminals.stop_session(session_id)', (ROOT / 'services/forge_sessions.py').read_text(encoding='utf-8'))
        self.assertNotIn('forge_terminal', (ROOT / 'core/tool_registry.py').read_text(encoding='utf-8'))


if '--integration' in sys.argv:
    sys.argv.remove('--integration')
    import tempfile
    class RealPtyTests(unittest.TestCase):
        def test_os_pty_round_trip(self):
            with tempfile.TemporaryDirectory(prefix='forge-pty-') as folder:
                sessions = SimpleNamespace(lock=threading.RLock(), workspace=lambda _id: Path(folder), get=lambda _id: {})
                service = ForgeTerminals(sessions); self.addCleanup(service.shutdown)
                id = service.start('fixture', is_admin=True)['id']
                service.resize('fixture', id, 30, 100)
                service.write('fixture', id, 'echo FORGE_PTY_TEST\r\n')
                for _ in range(200):
                    text = ''.join(e['data'] for e in service.output('fixture', id)['events'])
                    if 'FORGE_PTY_TEST' in text: break
                    time.sleep(.05)
                self.assertIn('FORGE_PTY_TEST', text)
                service.stop_session('fixture'); self.assertFalse(service.records)

if __name__ == '__main__': unittest.main()
