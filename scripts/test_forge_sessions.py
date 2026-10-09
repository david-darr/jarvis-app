"""Forge Phase 2 Unit A contracts. Real local Git; no model CLI or network."""
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
environment = tempfile.TemporaryDirectory(prefix='kairos-forge-sessions-')
os.environ['JARVIS_DATA_DIR'] = str(Path(environment.name) / 'app-data')

import httpx
from fastapi import FastAPI
from core import file_checkpoints, middleware, session_manager_store, tool_registry
from core.session_manager import session_manager
from routes import forge_routes
from services import chat_service, forge_git, forge_projects as projects_module
from services import forge_sessions as sessions_module


def tearDownModule():
    session_manager_store.close()
    environment.cleanup()


class ForgeSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-session-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'project folder'
        self.repo.mkdir()
        self.forge_root = self.root / 'forge'
        self.projects = projects_module.ForgeProjects(self.root / 'projects.json')
        self.service = sessions_module.ForgeSessions(self.projects)
        self.addCleanup(session_manager_store.delete_all_sessions)
        self.setting = patch.object(projects_module, 'get_setting', return_value=str(self.forge_root))
        self.setting.start(); self.addCleanup(self.setting.stop)
        self.endpoint = patch.object(sessions_module.model_endpoints, 'get_endpoint',
                                     return_value={'id': 'chosen', 'kind': 'claude_cli', 'model': 'fixture'})
        self.endpoint.start(); self.addCleanup(self.endpoint.stop)
        self.git('init', '-b', 'main')
        self.original = ''.join(f'line {i}\n' for i in range(40))
        self.write(self.repo / 'code.txt', self.original)
        self.write(self.repo / 'AGENTS.md', 'Repository agent rules\n')
        self.write(self.repo / 'CLAUDE.md', 'Repository Claude rules\n')
        self.git('add', '.'); self.git('commit', '-m', 'Seed')
        self.project = self.projects.add(str(self.repo), 'Fixture')

    def git(self, *args, cwd=None):
        env = forge_git.git_env()
        env.update(GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test',
                   GIT_AUTHOR_DATE='2026-10-01T12:00:00+00:00', GIT_COMMITTER_DATE='2026-10-01T12:00:00+00:00')
        # Same line-ending settings as Forge's own runner, or a fresh worktree
        # looks fully modified on machines with core.autocrlf=true.
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
                                 '-c', 'commit.gpgsign=false', '-c', 'protocol.ext.allow=never', *args],
                                cwd=cwd or self.repo, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    @staticmethod
    def write(path, text):
        path.write_text(text, encoding='utf-8', newline='')

    def create(self, **kwargs):
        return self.service.create(self.project['id'], 'Fix garden', 'chosen', **kwargs)

    def test_session_starts_with_the_exact_model_chosen(self):
        session = self.create(model_override='claude-sonnet-5')
        self.assertEqual(session_manager.get_session(session['id'])['model_override'], 'claude-sonnet-5')
        self.assertIsNone(session_manager.get_session(self.create(model_override='  ')['id'])['model_override'])
        for bad in ('-flag', 'a b', 'x' * 161, 'model;rm'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'valid model ID'):
                self.create(model_override=bad)
        # Same rule as Chat: exact models only for CLI and API agents.
        with patch.object(sessions_module.model_endpoints, 'get_endpoint', return_value={'id': 'chosen', 'kind': 'local', 'model': 'm'}):
            with self.assertRaisesRegex(ValueError, 'CLI and API'):
                self.create(model_override='claude-sonnet-5')

    def test_new_worktree_chat_metadata_and_listing(self):
        session = self.create()
        details = session['forge']; folder = Path(session['workspace_dir'])
        self.assertEqual(folder.parent, self.forge_root / '.worktrees' / (self.repo.name + '-' + self.project['id']))
        self.assertRegex(folder.name, r'^fix-garden-[0-9a-f]{6}$')
        self.assertEqual(details['branch'], 'forge/' + folder.name)
        self.assertEqual(details['base_commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(details['base_branch'], 'main')
        self.assertEqual(session['model_endpoint_id'], 'chosen')
        self.assertEqual(session['title'], 'Fix garden')
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertTrue(next(s for s in session_manager.list_sessions() if s['id'] == session['id'])['is_forge'])
        self.assertEqual(self.service.list(self.project['id'])[0]['id'], session['id'])
        self.assertEqual(self.service.workspace(session['id']), folder)
        with self.assertRaises(ValueError): session_manager.set_workspace(session['id'], str(self.repo))

    def test_existing_branch_and_in_place(self):
        self.git('branch', 'topic')
        session = self.create(isolation='existing_branch', branch='topic')
        self.assertEqual(self.git('branch', '--show-current', cwd=session['workspace_dir']), 'topic')
        self.assertEqual(session['forge']['base_branch'], 'main')
        self.assertTrue(next(b for b in self.service.branches(self.project['id']) if b['name'] == 'topic')['worktree'])
        with self.assertRaises(forge_git.GitError): self.create(isolation='existing_branch', branch='topic')
        inplace = self.create(isolation='in_place')
        self.assertEqual(inplace['workspace_dir'], str(self.repo))
        self.write(self.repo / 'keep.txt', 'Keep me')
        self.service.remove(inplace['id'], discard=True, confirmed=True)
        self.assertEqual((self.repo / 'keep.txt').read_text(), 'Keep me')
        self.assertTrue(self.repo.is_dir())

    def test_path_vetting_and_escape_refusal(self):
        session = self.create()
        for name in ('../code.txt', '/code.txt', '.git/config', 'C:/escape', 'dir/../code.txt', 'file:stream', 'NUL', 'file.', 'dir\\file'):
            with self.subTest(name=name), self.assertRaises(ValueError): self.service.file(session['id'], name)
        with self.assertRaises(ValueError): self.service._destination(self.forge_root, self.repo, '../..', 'escape')
        with self.assertRaises(ValueError): self.service._destination(self.repo, self.repo, 'project', 'leaf')
        with self.assertRaises(ValueError): projects_module.vet_project(str(ROOT))
        with self.assertRaises(forge_git.GitError): forge_git.validate_branch(self.repo, '--bad')
        with self.assertRaises(forge_git.GitError): forge_git.validate_branch(self.repo, 'bad..name')
        details = {**session['forge'], 'worktree': str(self.root / 'escape')}
        session_manager.set_forge(session['id'], details)
        with self.assertRaises(ValueError): self.service.workspace(session['id'])

    def test_changes_untracked_binary_lazy_tree_and_file_caps(self):
        session = self.create(); folder = Path(session['workspace_dir'])
        self.write(folder / 'code.txt', self.original.replace('line 1\n', 'changed\n'))
        (folder / 'nested').mkdir()
        self.write(folder / 'nested' / 'new file.txt', 'new\ntext')
        (folder / 'binary.bin').write_bytes(b'\x00binary')
        (folder / '.gitignore').write_text('ignored.txt\n')
        self.write(folder / 'ignored.txt', 'ignored')
        changes = {r['path']: r for r in self.service.changes(session['id'])['files']}
        self.assertEqual((changes['code.txt']['added'], changes['code.txt']['removed']), (1, 1))
        self.assertEqual(changes['nested/new file.txt']['added'], 2)
        self.assertTrue(changes['nested/new file.txt']['untracked'])
        self.assertTrue(changes['binary.bin']['binary'])
        self.assertNotIn('ignored.txt', changes)
        tree = {r['path']: r for r in self.service.files(session['id'])['entries']}
        self.assertTrue(tree['nested']['changed']); self.assertTrue(tree['nested']['directory'])
        self.assertNotIn('nested/new file.txt', tree); self.assertNotIn('.git', tree)
        self.assertEqual(self.service.files(session['id'], 'nested')['entries'][0]['path'], 'nested/new file.txt')
        with self.assertRaises(ValueError): self.service.revert_file(session['id'], 'nested', True)
        self.assertEqual(self.service.file(session['id'], 'nested/new file.txt')['content'], 'new\ntext')
        self.assertTrue(self.service.file(session['id'], 'binary.bin')['binary'])
        (folder / 'large.txt').write_bytes(b'x' * (sessions_module.TEXT_CAP + 1))
        with self.assertRaisesRegex(ValueError, 'size'): self.service.file(session['id'], 'large.txt')
        with patch.object(sessions_module, '_linked', side_effect=lambda p: p.name == 'nested'):
            with self.assertRaisesRegex(ValueError, 'Links'): self.service.file(session['id'], 'nested/new file.txt')
            with self.assertRaisesRegex(ValueError, 'Links'): self.service.revert_file(session['id'], 'nested/new file.txt', True)

    def test_single_hunk_revert_stale_hash_and_file_revert(self):
        session = self.create(); folder = Path(session['workspace_dir']); path = folder / 'code.txt'
        modified = self.original.replace('line 1\n', 'first change\n').replace('line 35\n', 'second change\n')
        self.write(path, modified)
        row = self.service.changes(session['id'])['files'][0]
        self.assertEqual(len(row['hunks']), 2)
        self.assertEqual(row, self.service.changes(session['id'])['files'][0])
        with self.assertRaisesRegex(ValueError, 'confirmed'): self.service.revert_hunk(session['id'], 'code.txt', row['hunks'][0]['hash'])
        self.service.revert_hunk(session['id'], 'code.txt', row['hunks'][0]['hash'], True)
        self.assertEqual(path.read_text(), self.original.replace('line 35\n', 'second change\n'))
        with self.assertRaises(sessions_module.ReviewConflict): self.service.revert_hunk(session['id'], 'code.txt', row['hunks'][0]['hash'], True)
        self.service.revert_file(session['id'], 'code.txt', True)
        self.assertEqual(path.read_text(), self.original)
        self.assertEqual((self.repo / 'code.txt').read_text(), self.original)
        self.write(folder / 'new file.txt', 'hello\nworld')
        new = next(r for r in self.service.changes(session['id'])['files'] if r['path'] == 'new file.txt')
        self.service.revert_hunk(session['id'], 'new file.txt', new['hunks'][0]['hash'], True)
        self.assertFalse((folder / 'new file.txt').exists())
        self.write(folder / 'new.txt', 'new')
        self.service.revert_file(session['id'], 'new.txt', True)
        self.assertFalse((folder / 'new.txt').exists())

    def test_remove_dirty_confirmed_and_keep_branch(self):
        session = self.create(); folder = Path(session['workspace_dir'])
        self.write(folder / 'new.txt', 'dirty')
        with self.assertRaises(sessions_module.ReviewConflict): self.service.remove(session['id'])
        with self.assertRaises(ValueError): self.service.remove(session['id'], discard=True)
        self.service.remove(session['id'], discard=True, confirmed=True)
        self.assertFalse(folder.exists())
        self.assertIn(session['forge']['branch'], self.git('branch', '--list'))
        self.assertTrue(self.service.get(session['id'])['forge']['removed'])
        clean = self.create(); clean_folder = Path(clean['workspace_dir'])
        self.service.remove(clean['id'])
        self.assertFalse(clean_folder.exists())

    def test_tracked_additions_and_staged_deletions_revert_from_base(self):
        session = self.create(); sid = session['id']; folder = Path(session['workspace_dir'])
        self.write(folder / 'added.txt', 'staged addition\n')
        self.git('add', '--', 'added.txt', cwd=folder)
        self.service.revert_file(sid, 'added.txt', True)
        self.assertFalse((folder / 'added.txt').exists())
        self.assertEqual(self.git('status', '--porcelain', cwd=folder), '')
        self.git('rm', '--', 'code.txt', cwd=folder)
        self.service.revert_file(sid, 'code.txt', True)
        self.assertEqual((folder / 'code.txt').read_text(), self.original)
        self.assertEqual(self.git('status', '--porcelain', cwd=folder), '')

    def test_plan_flags_tools_dispatch_and_mode_switch(self):
        session = self.create(mode='plan'); sid = session['id']
        session_manager.set_permission_mode(sid, 'auto')
        with patch.object(sessions_module, 'forge_sessions', self.service), \
             patch.object(chat_service.model_endpoints, 'resolve_runtime', return_value=('http://fixture', 'model', None, 8192)):
            claude = chat_service._build_brain({'id': 'chosen', 'kind': 'claude_cli'}, sid, True)
            with patch.object(claude, '_tool_config', return_value=([], [], {})), \
                 patch('core.claude_cli.preferred_cli_path', return_value='claude'), \
                 patch('core.hive_mind_server.get_hive_mind_server', return_value={}):
                self.assertEqual(claude._options().permission_mode, 'plan')
            refused = asyncio.run(claude._permission('Write', {}, SimpleNamespace()))
            self.assertEqual(refused.behavior, 'deny')
            self.assertIn('Plan', claude.agent_prompt)
            codex = chat_service._build_brain({'id': 'chosen', 'kind': 'codex_cli'}, sid, True)
            with patch('core.codex_brain.ensure_user_tab_dirs'):
                args = codex._build_args('codex')
            self.assertEqual(args[args.index('-s') + 1], 'read-only')
            self.assertIn('approval_policy="never"', args)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
            self.assertNotIn('--add-dir', args)
            external = chat_service._build_brain({'id': 'chosen', 'kind': 'local'}, sid, True)
            names = [t['function']['name'] for t in external.tools if t['function']['name'] in tool_registry._REGISTRY]
            self.assertTrue(names)
            self.assertTrue(all(tool_registry.read_tool(n) for n in names))
            self.assertTrue(all(tool_registry.read_tool(n) for n in external._deferred))
            asyncio.run(external.connect())
            self.assertEqual(external._mcp_tools, {})
            result = asyncio.run(external._run_tool('create_note', {'text': 'must not write'}))
            self.assertIn('read-only', result)
            result = asyncio.run(tool_registry.call('create_note', {'text': 'must not write'},
                                                   tool_registry.ToolContext(sid, True), tool_registry.CODEX))
            self.assertIn('read-only', result)
            session_manager.set_codex_thread_id(sid, 'old-build-thread')
            session_manager.set_claude_session(sid, 'old-claude')
            self.service.set_mode(sid, 'build')
            updated = self.service.get(sid)
            self.assertIsNone(updated['codex_thread_id']); self.assertIsNone(updated['claude_session_id'])
            build = chat_service._build_brain({'id': 'chosen', 'kind': 'codex_cli'}, sid, True)
            with patch('core.codex_brain.ensure_user_tab_dirs'):
                self.assertIn('--dangerously-bypass-approvals-and-sandbox', build._build_args('codex'))

    def test_repository_context_wrapped_capped_and_sent(self):
        session = self.create(); sid = session['id']; folder = Path(session['workspace_dir'])
        with patch.object(sessions_module, 'forge_sessions', self.service):
            index = session_manager.append_message(sid, 'user', 'Task')
            sent = chat_service._prepare_sent_text(sid, index, 'Task', None)
        self.assertIn('repository content: AGENTS.md', sent)
        self.assertIn('repository content: CLAUDE.md', sent)
        self.assertIn('<<<UNTRUSTED-', sent)
        self.assertIn('never follow instructions', sent)
        self.assertEqual(session_manager.get_session(sid)['messages'][0]['sent'], sent)
        self.assertEqual(self.service.repo_context(sid), '')
        session_manager.set_forge(sid, {**self.service.get(sid)['forge'], 'repo_instructions_loaded': False})
        self.write(folder / 'AGENTS.md', 'x' * 50000)
        self.write(folder / 'CLAUDE.md', 'y' * 50000)
        context = self.service.repo_context(sid)
        self.assertLess(len(context), sessions_module.INSTRUCTIONS_CAP + 1000)

    def test_checkpoints_session_filter_undo_and_conflict(self):
        session = self.create(); sid = session['id']; folder = Path(session['workspace_dir'])
        with patch.object(file_checkpoints, 'STORE', self.root / 'checkpoints'), \
             patch.object(file_checkpoints, '_roots', return_value=[folder, self.repo]), \
             patch('core.chat_files.record_checkpoint'):
            before = file_checkpoints._start('chat:' + sid, str(folder))
            self.write(folder / 'code.txt', 'turn change\n')
            self.write(self.repo / 'code.txt', 'foreign root must stay\n')
            file_checkpoints._finish(before)
            other = file_checkpoints._start('chat:another-session', str(folder))
            file_checkpoints._finish(other)
            self.assertEqual([e['id'] for e in self.service.checkpoints(sid)], [before['id']])
            with self.assertRaises(KeyError): self.service.undo(sid, other['id'], True)
            self.assertEqual(len(self.service.undo(sid, before['id'], True)['restored']), 1)
            self.assertEqual((folder / 'code.txt').read_text(), self.original)
            self.assertEqual((self.repo / 'code.txt').read_text(), 'foreign root must stay\n')
            conflict = file_checkpoints._start('chat:' + sid, str(folder))
            self.write(folder / 'code.txt', 'after\n'); file_checkpoints._finish(conflict)
            self.write(folder / 'code.txt', 'changed again\n')
            with self.assertRaises(file_checkpoints.Conflict): self.service.undo(sid, conflict['id'], True)
            self.assertEqual((folder / 'code.txt').read_text(), 'changed again\n')

    def test_routes_admin_confirmation_conflicts_and_busy(self):
        app = FastAPI(); app.include_router(forge_routes.router)
        session = self.create(); sid = session['id']
        async def requests():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with patch.object(middleware, 'require_user', return_value='member'), \
                     patch.object(middleware.auth_manager, 'is_admin', return_value=False):
                    for route in forge_routes.router.routes:
                        url = route.path.replace('{project_id}', self.project['id']).replace('{session_id}', sid).replace('{event_id}', 'a' * 32)
                        response = await client.request(next(iter(route.methods)), url, json={})
                        self.assertEqual(response.status_code, 403, url)
                app.dependency_overrides[middleware.require_admin] = lambda: 'admin'
                with patch.object(forge_routes, 'forge_sessions', self.service):
                    self.assertEqual((await client.get(f'/api/forge/sessions/{sid}')).status_code, 200)
                    self.assertEqual((await client.get(f'/api/forge/sessions/{sid}/file', params={'path': '../escape'})).status_code, 400)
                    url = f'/api/forge/sessions/{sid}/revert-file'
                    self.assertEqual((await client.post(url, json={'path': 'code.txt'})).status_code, 400)
                    self.assertEqual((await client.post(url, json={'path': 'code.txt', 'confirmed': 'true'})).status_code, 422)
                    self.assertEqual((await client.post(url, json={'path': 'code.txt', 'confirmed': True})).status_code, 200)
                    self.assertEqual((await client.post(f'/api/forge/sessions/{sid}/revert-hunk', json={'path': 'code.txt', 'hunk_hash': 'stale', 'confirmed': True})).status_code, 409)
                    self.write(Path(session['workspace_dir']) / 'dirty.txt', 'dirty')
                    self.assertEqual((await client.delete(f'/api/forge/sessions/{sid}')).status_code, 409)
                    self.assertEqual((await client.delete(f'/api/forge/sessions/{sid}?discard=true')).status_code, 400)
                    chat_service._busy.add(sid)
                    try:
                        self.assertEqual((await client.post(f'/api/forge/sessions/{sid}/mode', json={'mode': 'plan'})).status_code, 409)
                    finally:
                        chat_service._busy.discard(sid)
                    with patch.object(self.service, 'undo', side_effect=file_checkpoints.Conflict('Changed since checkpoint')):
                        response = await client.post(f'/api/forge/sessions/{sid}/checkpoints/' + 'a' * 32 + '/undo', json={'confirmed': True})
                        self.assertEqual(response.status_code, 409)
                        self.assertIn('Changed since', response.json()['detail'])
        asyncio.run(requests())


if __name__ == '__main__':
    unittest.main()
