"""Forge App Preview contracts. Local Git and HTTP fixtures only, no network services."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
environment = tempfile.TemporaryDirectory(prefix='kairos-forge-apps-')
os.environ['JARVIS_DATA_DIR'] = str(Path(environment.name) / 'app-data')

import httpx
from fastapi import FastAPI
from core import middleware, permissions, session_manager_store, tool_registry
from core.session_manager import session_manager
from services import forge_apps as apps_module, forge_projects as projects_module, forge_sessions as sessions_module
from routes import forge_routes

FIXTURE = '''import http.server, json, os, subprocess, sys
from pathlib import Path
port = int(sys.argv[1])
Path('child-env.json').write_text(json.dumps(dict(os.environ)))
if '--tree' in sys.argv:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
    Path('child-pid.txt').write_text(str(child.pid))
if '--many' in sys.argv:
    for i in range(2200): print('line ' + str(i), flush=True)
if '--url' in sys.argv:
    port = int(sys.argv[sys.argv.index('--url') + 1])
print('Local: http://localhost:' + str(port) + '/', flush=True)
print('Rejected backend: http://127.0.0.1:' + sys.argv[2] + '/', flush=True)
http.server.ThreadingHTTPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler).serve_forever()
'''


def tearDownModule():
    session_manager_store.close()
    environment.cleanup()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-app-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo with spaces'; self.repo.mkdir()
        self.projects = projects_module.ForgeProjects(self.root / 'projects.json')
        self.sessions = sessions_module.ForgeSessions(self.projects)
        self.apps = apps_module.ForgeApps(self.sessions, backend_port=8420, timeout=4)
        self.addCleanup(self.apps.shutdown)
        self.setting = patch.object(projects_module, 'get_setting', return_value=str(self.root / 'forge'))
        self.setting.start(); self.addCleanup(self.setting.stop)
        self.global_apps = patch.object(apps_module, 'forge_apps', self.apps)
        self.global_apps.start(); self.addCleanup(self.global_apps.stop)
        self.route_apps = patch.object(forge_routes, 'forge_apps', self.apps)
        self.route_apps.start(); self.addCleanup(self.route_apps.stop)
        self.route_sessions = patch.object(forge_routes, 'forge_sessions', self.sessions)
        self.route_sessions.start(); self.addCleanup(self.route_sessions.stop)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        for args in [('init', '-b', 'main'), ('add', '.'), ('commit', '-m', 'Fixture')]:
            if args[0] == 'add':
                (self.repo / 'serve.py').write_text(FIXTURE)
                (self.repo / 'package.json').write_text(json.dumps({'scripts': {'dev': 'vite', 'start': 'next start', 'preview': 'vite preview'}}))
            result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false', *args],
                                    cwd=self.repo, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.project = self.projects.add(str(self.repo))
        self.session = self.sessions.create(self.project['id'], 'Preview fixture')
        self.sid = self.session['id']; self.worktree = Path(self.session['workspace_dir'])
        self.command = f'"{sys.executable}" -u serve.py {{port}} 8420'
        self.apps.set_command(self.project['id'], self.command)

    def start(self, command=None):
        if command:
            self.apps.set_command(self.project['id'], command)
        return asyncio.run(self.apps.start(self.sid, True))

    def approved(self):
        return patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('allow')))

    def wait(self, condition):
        for _ in range(100):
            if condition(): return
            time.sleep(.05)
        self.fail('Fixture did not settle')

    def test_suggestions_runners_and_frameworks_and_saved_command(self):
        result = self.apps.suggest(self.sid)
        self.assertEqual(result['command'], self.command)
        self.assertEqual(len(result['suggestions']), 3)
        self.assertTrue(result['suggestions'][0].startswith('npm run dev'))
        self.assertIn('--port {port}', result['suggestions'][0])
        for lock, runner in [('pnpm-lock.yaml', 'pnpm'), ('yarn.lock', 'yarn'), ('bun.lock', 'bun')]:
            (self.worktree / lock).write_text('fixture')
            self.assertTrue(self.apps.suggest(self.sid)['suggestions'][0].startswith(runner))
            (self.worktree / lock).unlink()
        (self.worktree / 'manage.py').write_text('fixture')
        (self.worktree / 'app.py').write_text('from flask import Flask')
        (self.worktree / 'bin').mkdir(); (self.worktree / 'bin' / 'rails').write_text('fixture')
        commands = '\n'.join(self.apps.suggest(self.sid)['suggestions'])
        for part in ['manage.py runserver', '-m flask', 'ruby bin/rails server']:
            self.assertIn(part, commands)
        (self.worktree / 'package.json').write_text('{"dependencies":{"vite":"fixture"}}')
        self.assertIn('node node_modules/vite/bin/vite.js', '\n'.join(self.apps.suggest(self.sid)['suggestions']))
        (self.worktree / 'package.json').write_text('{"dependencies":{"next":"fixture"}}')
        self.assertIn('node node_modules/next/dist/bin/next', '\n'.join(self.apps.suggest(self.sid)['suggestions']))

    def test_approval_once_changed_command_again_denied_never_spawns(self):
        with self.approved() as decide:
            self.start(); self.apps.stop(self.sid); self.start()
            self.assertEqual(decide.await_count, 1)
            kwargs = decide.call_args.kwargs
            self.assertTrue(kwargs['force_prompt']); self.assertEqual(kwargs['arguments']['folder'], str(self.worktree))
            self.assertNotIn('{port}', kwargs['arguments']['command'])
            self.apps.stop(self.sid); self.start(self.command + ' --many')
            self.assertEqual(decide.await_count, 2)
        self.apps.stop(self.sid); self.apps.set_command(self.project['id'], self.command + ' --tree')
        with patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('deny', 'Declined'))), \
             patch.object(subprocess, 'Popen', wraps=subprocess.Popen) as spawn:
            with self.assertRaisesRegex(ValueError, 'Declined'): self.start()
            # Popen is patched globally, so Forge's own git reads also appear here.
            # A declined start must never launch anything other than git.
            launched = [c.args[0] for c in spawn.call_args_list if Path(str(c.args[0][0])).stem.lower() != 'git']
            self.assertEqual(launched, [], 'a declined app command must not spawn')

    def test_port_readiness_worktree_and_minimal_child_environment(self):
        with self.approved(), patch.dict(os.environ, {'JARVIS_FAKE': 'secret', 'OPENAI_API_KEY': 'secret',
                                                   'SECRET_TOKEN': 'secret', 'NODE_ENV': 'production'}):
            status = self.start()
        self.assertTrue(status['ready']); self.assertNotEqual(status['port'], 8420)
        with socket.create_connection(('127.0.0.1', status['port']), timeout=1): pass
        child = json.loads((self.worktree / 'child-env.json').read_text())
        self.assertEqual(child['PORT'], str(status['port'])); self.assertEqual(child['BROWSER'], 'none')
        for key in child:
            self.assertFalse(key.upper().startswith('JARVIS_'))
            self.assertNotIn(key.upper(), {'OPENAI_API_KEY', 'SECRET_TOKEN', 'NODE_ENV'})
        self.assertFalse((self.repo / 'child-env.json').exists())
        passed = apps_module.child_env(status['port'])
        self.assertEqual(set(k.upper() for k in passed) - apps_module.ENV_KEYS, {'PORT', 'BROWSER'})

    def test_output_url_detection_backend_refusal_and_log_cap(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); alternate = sock.getsockname()[1]
        with self.approved(): status = self.start(self.command + f' --url {alternate} --many')
        self.assertEqual(status['port'], alternate); self.assertIn(alternate, status['ports'])
        self.assertNotIn(8420, status['ports'])
        self.wait(lambda: len(self.apps.logs(self.sid, 2000)['lines']) == 2000)
        lines = self.apps.logs(self.sid, 2000)['lines']; self.assertEqual(len(lines), 2000)
        self.assertTrue(lines[0].startswith('line ')); self.assertNotIn('line 0', lines)
        self.assertEqual(forge_routes.app_allowed_ports(self.sid)['ports'], status['ports'])

    def test_stop_restart_end_remove_and_shutdown_kill_tree(self):
        with self.approved():
            status = self.start(self.command + ' --tree')
            record = self.apps.records[self.sid]; pid = int((self.worktree / 'child-pid.txt').read_text())
            self.apps.stop(self.sid)
            self.assertIsNotNone(record['process'].poll())
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, pid)
                if handle:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0); kernel.CloseHandle(handle)
            else:
                def dead():
                    try:
                        state = Path(f'/proc/{pid}/stat')
                        if state.exists() and state.read_text().split()[2] == 'Z': return True
                        os.kill(pid, 0); return False
                    except ProcessLookupError: return True
                self.wait(dead)
            self.assertEqual(self.apps.status(self.sid)['ports'], [])
            restarted = asyncio.run(self.apps.restart(self.sid, True)); self.assertTrue(restarted['ready'])
            asyncio.run(forge_routes.end_session(self.sid)); self.assertFalse(self.apps.status(self.sid)['running'])
            self.start(); self.sessions.remove(self.sid, discard=True, confirmed=True)
            self.assertFalse(self.worktree.exists()); self.assertFalse(self.apps.status(self.sid)['running'])
            other = self.sessions.create(self.project['id'], 'shutdown fixture', isolation='in_place')
            asyncio.run(self.apps.start(other['id'], True)); self.apps.shutdown()
            self.assertFalse(self.apps.status(other['id'])['running'])

    def test_readiness_timeout_and_shell_refusal(self):
        self.apps.timeout = .2
        with self.approved(), self.assertRaisesRegex(ValueError, 'ready'):
            self.start(f'"{sys.executable}" -c "import time; time.sleep(20)"')
        self.assertFalse(self.apps.status(self.sid)['running'])
        for command in ['npm run dev && echo done', 'cmd /c npm run dev', 'bash -c foo', 'echo hi > file', 'npm run dev; whoami']:
            with self.assertRaises(ValueError): apps_module.command_args(command)

    def test_windows_cmd_resolution_without_shell(self):
        folder = self.root / 'node install'; folder.mkdir()
        node = folder / 'node.exe'; node.write_text('fixture')
        for runner, entry in [('npm', 'npm/bin/npm-cli.js'), ('pnpm', 'pnpm/bin/pnpm.cjs'), ('yarn', 'yarn/bin/yarn.js')]:
            shim = folder / (runner + '.cmd'); shim.write_text('ignored batch syntax')
            script = folder / 'node_modules' / entry; script.parent.mkdir(parents=True, exist_ok=True); script.write_text('fixture')
            with patch.object(apps_module.shutil, 'which', return_value=str(shim)):
                argv = apps_module.resolve_command([runner, 'run', 'dev'], {'PATH': str(folder)}, windows=True)
            self.assertEqual(argv, [str(node), str(script), 'run', 'dev'])
        unknown = folder / 'custom.cmd'; unknown.write_text('fixture')
        with patch.object(apps_module.shutil, 'which', return_value=str(unknown)), self.assertRaisesRegex(ValueError, 'shell'):
            apps_module.resolve_command(['custom'], {}, windows=True)

    def test_admin_routes_and_own_session_log_tool(self):
        app = FastAPI(); app.include_router(forge_routes.router)
        async def requests():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with patch.object(middleware, 'require_user', return_value='member'), patch.object(middleware.auth_manager, 'is_admin', return_value=False):
                    for route in forge_routes.router.routes:
                        if '/app' not in route.path and not route.path.endswith('/end'): continue
                        url = route.path.replace('{session_id}', self.sid).replace('{project_id}', self.project['id'])
                        self.assertEqual((await client.request(next(iter(route.methods)), url, json={})).status_code, 403, url)
                app.dependency_overrides[middleware.require_admin] = lambda: 'admin'
                self.assertEqual((await client.get(f'/api/forge/sessions/{self.sid}/app/status')).status_code, 200)
        asyncio.run(requests())
        with self.assertRaisesRegex(ValueError, 'admin'): asyncio.run(self.apps.start(self.sid))
        other = session_manager.create_session('ordinary')['id']
        self.assertNotIn('forge_app_logs', [s.name for s in tool_registry.specs(tool_registry.CODEX, True, session_id=other)])
        self.assertIn('forge_app_logs', [s.name for s in tool_registry.specs(tool_registry.CODEX, True, session_id=self.sid)])
        for ctx in [tool_registry.ToolContext(other, True), tool_registry.ToolContext(self.sid, False)]:
            self.assertEqual(asyncio.run(tool_registry.call('forge_app_logs', {}, ctx, tool_registry.CODEX)), 'Unknown tool: forge_app_logs')
        with self.approved(): self.start()
        own = asyncio.run(tool_registry.call('forge_app_logs', {'session_id': other}, tool_registry.ToolContext(self.sid, True), tool_registry.CODEX))
        self.assertIn('Local:', own)
        second = self.sessions.create(self.project['id'], 'other Forge session')
        reply = asyncio.run(tool_registry.call('forge_app_logs', {'session_id': self.sid}, tool_registry.ToolContext(second['id'], True), tool_registry.CODEX))
        self.assertEqual(reply, 'No app logs for this session.')


class ApprovalTests(unittest.TestCase):
    def test_auto_and_standing_rules_do_not_bypass_first_run(self):
        sid = session_manager.create_session('Auto fixture')['id']
        session_manager.set_permission_mode(sid, 'auto')
        async def approval():
            surface = 'chat:' + sid
            queue = permissions.open_channel(surface)
            try:
                with patch.object(permissions, 'stored_decision', return_value=permissions.Decision('allow')):
                    pending = asyncio.create_task(permissions.decide(surface=surface, tool='forge_app_start',
                        arguments={'command': 'fixture', 'folder': 'fixture'}, force_prompt=True, is_admin=True, choices=apps_module.CHOICES))
                    request = await asyncio.wait_for(queue.get(), 1)
                    self.assertFalse(pending.done()); permissions.answer(request['id'], 'reject', 'owner')
                    self.assertEqual((await pending).behavior, 'deny')
            finally: permissions.close_channel(surface)
        asyncio.run(approval())


if __name__ == '__main__':
    unittest.main()
