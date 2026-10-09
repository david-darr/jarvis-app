"""Offline contracts. --integration adds temporary, real Git repository fixtures."""
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['JARVIS_DATA_DIR'] = str(ROOT / 'data' / 'forge-first-commit-unit-tests')

from core import permissions
from services import forge_git as git, forge_projects as projects, forge_sessions as sessions
from services.forge_session_git import ForgeSessionGit


class FirstCommitTests(unittest.TestCase):
    def test_identity_is_resolved_and_fallback_is_command_only(self):
        for name, email, fallback in [('Person', 'person@example.test', False), ('', '', True), ('Person', '', True)]:
            def run(_path, args, **kwargs):
                if args == ['config', '--get', 'user.name']: return (0 if name else 1), name
                if args == ['config', '--get', 'user.email']: return (0 if email else 1), email
                return 0, ''
            with self.subTest(name=name, email=email), patch.object(git, 'has_commits', return_value=False), \
                 patch.object(git, 'run_git', return_value=(0, '?? README.md\0?? .gitignore\0')), \
                 patch.object(git, '_run', side_effect=run) as runner:
                git.initial_commit(ROOT, ['README.md', '.gitignore'])
                args = [c.args[1] for c in runner.call_args_list]
                self.assertEqual(args[:2], [['config', '--get', 'user.name'], ['config', '--get', 'user.email']])
                self.assertEqual(args[2], ['add', '-A', '--', 'README.md', '.gitignore'])
                self.assertEqual('user.name=Kairos' in args[-1], fallback)
                self.assertEqual('user.email=kairos@localhost' in args[-1], fallback)
                self.assertEqual(args[-1][-3:], ['commit', '-m', 'Initial commit'])
                self.assertNotIn('hooks', runner.call_args.kwargs)
                self.assertTrue(all(a[:2] == ['config', '--get'] for a in args if a[0] == 'config'))

    def test_new_commit_failure_removes_reserved_folder(self):
        root = MagicMock(); folder = MagicMock(); root.__truediv__.return_value = folder
        service = projects.ForgeProjects(); service.add = Mock()
        with patch.object(projects, 'forge_root', return_value=root), patch.object(git, 'init_repo'), \
             patch.object(git, 'initial_commit', side_effect=git.GitError('Commit failed')), \
             patch.object(git, 'remove_reserved') as remove:
            folder.__truediv__.return_value = MagicMock()
            with self.assertRaisesRegex(git.GitError, 'Commit failed'): service.new('Garden')
            remove.assert_called_once_with(folder)
            service.add.assert_not_called()

    def test_unborn_refused_before_revision_branch_or_worktree_for_all_modes(self):
        service = sessions.ForgeSessions(sessions=Mock())
        service.project = Mock(return_value=({'name': 'Garden'}, ROOT))
        for mode in ('new_worktree', 'existing_branch', 'in_place'):
            with self.subTest(mode=mode), patch.object(git, 'has_commits', return_value=False), \
                 patch.object(git, 'rev_parse') as revision, patch.object(git, 'worktree_add') as worktree, \
                 patch.object(git, 'validate_branch') as branch, patch.object(projects, 'forge_root') as root:
                with self.assertRaisesRegex(sessions.NoCommits, '^Garden has no commits yet. Make a first commit to start a session.$'):
                    service.create('p', 'Keep my task', isolation=mode, branch='topic')
                for fn in (revision, worktree, branch, root): fn.assert_not_called()
                service.sessions.create_session.assert_not_called()

    def test_first_commit_prompts_and_denial_does_not_even_stage(self):
        store = Mock(); store.lock = threading.RLock()
        store.project.return_value = ({'name': 'Garden'}, ROOT)
        service = ForgeSessionGit(store)
        for behavior in ('deny', 'allow'):
            with self.subTest(behavior=behavior), patch.object(git, 'has_commits', return_value=False), \
                 patch.object(git, 'run_git', return_value=(0, '?? README.md\0?? space file.txt\0')), \
                 patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision(behavior))) as decide, \
                 patch.object(git, 'initial_commit') as commit:
                if behavior == 'deny':
                    with self.assertRaises(ValueError): asyncio.run(service.first_commit('p', 'test'))
                    commit.assert_not_called()
                else:
                    self.assertEqual(asyncio.run(service.first_commit('p', 'test')), {'ok': True})
                    commit.assert_called_once_with(str(ROOT))
                self.assertTrue(decide.call_args.kwargs['force_prompt'])
                self.assertIn(str(ROOT), decide.call_args.kwargs['description'])
                self.assertIn('Files to commit: 2', decide.call_args.kwargs['description'])

    def test_first_commit_refuses_existing_empty_and_changed_repositories(self):
        store = Mock(); store.lock = threading.RLock()
        store.project.return_value = ({'name': 'Garden'}, ROOT)
        service = ForgeSessionGit(store)
        with patch.object(git, 'has_commits', return_value=True), patch.object(permissions, 'decide', new=AsyncMock()) as decide:
            with self.assertRaisesRegex(ValueError, 'already has commits'): asyncio.run(service.first_commit('p', 'test'))
            decide.assert_not_called()
        with patch.object(git, 'has_commits', return_value=False), patch.object(git, 'run_git', return_value=(0, '')):
            with self.assertRaisesRegex(ValueError, 'no files'): asyncio.run(service.first_commit('p', 'test'))
        with patch.object(git, 'has_commits', return_value=False), \
             patch.object(git, 'run_git', side_effect=[(0, '?? one\0'), (0, '?? two\0')]), \
             patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('allow'))), \
             patch.object(git, 'initial_commit') as commit:
            with self.assertRaisesRegex(sessions.ReviewConflict, 'changed during approval'):
                asyncio.run(service.first_commit('p', 'test'))
            commit.assert_not_called()

    def test_head_and_any_branch_commit_checks_are_distinct(self):
        with patch.object(git, 'run_git', return_value=(1, 'fatal: needed a single revision')) as read, \
             patch.object(git, '_run', return_value=(0, 'abc\n')) as runner:
            self.assertFalse(git.has_commits(ROOT))
            read.assert_called_once_with(ROOT, 'head', check=False)
            self.assertTrue(git.has_commits(ROOT, any_branch=True))
            self.assertEqual(runner.call_args.args[1], ['rev-list', '--all', '--max-count=1'])

    def test_real_permission_broker_emits_prompt_even_with_standing_allow(self):
        store = Mock(); store.lock = threading.RLock()
        store.project.return_value = ({'name': 'Garden'}, ROOT)
        service = ForgeSessionGit(store)
        async def exercise():
            surface = 'forge-first-commit:fixture'
            queue = permissions.open_channel(surface)
            task = asyncio.create_task(service.first_commit('p', surface))
            try:
                request = await asyncio.wait_for(queue.get(), timeout=2)
                self.assertEqual(request['title'], 'Make the first commit?')
                self.assertIn('Files to commit: 1', request['description'])
                self.assertEqual([c['id'] for c in request['choices']], ['once', 'reject'])
                permissions.answer(request['id'], 'reject', 'Person')
                with self.assertRaisesRegex(ValueError, 'not approved'): await task
            finally:
                permissions.close_channel(surface)
                if not task.done(): task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        with patch.object(git, 'has_commits', return_value=False), \
             patch.object(git, 'run_git', return_value=(0, '?? file\0')), \
             patch.object(permissions, 'stored_decision', return_value=permissions.Decision('allow')) as standing, \
             patch.object(permissions, '_load', return_value={'rules': [], 'audit': []}), \
             patch.object(permissions, '_save'), patch.object(git, 'initial_commit') as commit:
            asyncio.run(exercise())
            standing.assert_not_called(); commit.assert_not_called()

    def test_routes_return_plain_409_and_stream_project_approval(self):
        from fastapi import HTTPException
        from routes import forge_routes
        body = forge_routes.CreateForgeSessionRequest(project_id='p', task='Keep my task')
        with patch.object(forge_routes.forge_sessions, 'create', side_effect=sessions.NoCommits('Garden')):
            with self.assertRaises(HTTPException) as caught: forge_routes.create_forge_session(body, 'Person')
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(caught.exception.detail, 'Garden has no commits yet. Make a first commit to start a session.')
        async def exercise():
            async def launch(_project, surface):
                await permissions._channels[surface].put({'id': 'prompt', 'title': 'Make the first commit?'})
                return {'ok': True}
            with patch.object(forge_routes.forge_session_git, 'first_commit_target'), \
                 patch.object(forge_routes.forge_session_git, 'first_commit', side_effect=launch) as commit:
                response = await forge_routes.first_commit('p')
                packets = [packet async for packet in response.body_iterator]
                self.assertEqual(response.media_type, 'text/event-stream')
                self.assertIn('"permission"', packets[0])
                self.assertIn('"status": {"ok": true}', packets[1])
                commit.assert_called_once_with('p', 'forge-first-commit:p')
            with patch.object(forge_routes.forge_session_git, 'first_commit_target', side_effect=ValueError('This project already has commits.')):
                with self.assertRaises(HTTPException) as caught: await forge_routes.first_commit('p')
                self.assertEqual(caught.exception.status_code, 400)
        asyncio.run(exercise())
        self.assertTrue(any(route.path == '/api/forge/projects/{project_id}/first-commit' and route.dependencies
                            for route in forge_routes.router.routes))


if '--integration' in sys.argv:
    sys.argv.remove('--integration')

    class FirstCommitGitFixtureTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(prefix='forge-first-commit-fixture-')
            self.addCleanup(self.temp.cleanup)
            self.root = Path(self.temp.name)
            self.repo = self.root / 'project'; self.repo.mkdir()
            self.projects = projects.ForgeProjects(self.root / 'projects.json')
            self.service = sessions.ForgeSessions(self.projects)
            from core import session_manager_store
            self.addCleanup(session_manager_store.delete_all_sessions)
            # Ignore the machine's identity and signing setup without editing it.
            env = git.git_env()
            for key in list(env):
                if key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_', 'GIT_CONFIG')): env.pop(key)
            env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            self.env = patch.object(git, 'git_env', return_value=env)
            self.env.start(); self.addCleanup(self.env.stop)
            self.setting = patch.object(projects, 'get_setting', return_value=str(self.root / 'forge'))
            self.setting.start(); self.addCleanup(self.setting.stop)
            self.command('init', '-b', 'main')
            self.project = self.projects.add(str(self.repo), 'Garden')

        def command(self, *args, cwd=None, check=True):
            result = subprocess.run(['git', *args], cwd=cwd or self.repo, env=git.git_env(),
                capture_output=True, text=True, timeout=20)
            if check: self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        def test_new_has_exactly_one_initial_commit_and_cleanup_allows_retry(self):
            project = self.projects.new('New garden'); folder = Path(project['path'])
            self.assertEqual(self.command('rev-list', '--count', 'HEAD', cwd=folder), '1')
            self.assertEqual(self.command('ls-tree', '--name-only', 'HEAD', cwd=folder).splitlines(), ['.gitignore', 'README.md'])
            self.assertEqual(self.command('log', '-1', '--format=%s', cwd=folder), 'Initial commit')
            with patch.object(git, 'initial_commit', side_effect=git.GitError('Commit failed')):
                with self.assertRaises(git.GitError): self.projects.new('Retry garden')
            self.assertFalse((self.root / 'forge' / 'Retry-garden').exists())
            self.assertTrue(Path(self.projects.new('Retry garden')['path']).is_dir())

        def test_configured_and_fallback_identity_never_change_config(self):
            for configured in (False, True):
                repo = self.root / ('configured' if configured else 'fallback'); repo.mkdir()
                self.command('init', '-b', 'main', cwd=repo)
                if configured:
                    self.command('config', 'user.name', 'Person', cwd=repo)
                    self.command('config', 'user.email', 'person@example.test', cwd=repo)
                before = (repo / '.git' / 'config').read_bytes()
                (repo / 'file.txt').write_text('First file\n', encoding='utf-8')
                git.initial_commit(repo)
                expected = 'Person <person@example.test>' if configured else 'Kairos <kairos@localhost>'
                self.assertEqual(self.command('log', '-1', '--format=%an <%ae>', cwd=repo), expected)
                self.assertEqual(self.command('log', '-1', '--format=%cn <%ce>', cwd=repo), expected)
                self.assertEqual((repo / '.git' / 'config').read_bytes(), before)

        def test_unborn_all_modes_approval_denial_and_success(self):
            (self.repo / '.gitignore').write_text('ignored.txt\n', encoding='utf-8')
            (self.repo / 'ignored.txt').write_text('Ignore me', encoding='utf-8')
            (self.repo / 'space file.txt').write_text('Keep me', encoding='utf-8')
            worktrees = self.command('worktree', 'list', '--porcelain')
            for mode in ('new_worktree', 'existing_branch', 'in_place'):
                with patch.object(git, 'rev_parse') as revision, patch.object(git, 'worktree_add') as worktree:
                    with self.assertRaises(sessions.NoCommits):
                        self.service.create(self.project['id'], 'Task', isolation=mode, branch='topic')
                    revision.assert_not_called(); worktree.assert_not_called()
                self.assertEqual(self.command('branch'), '')
                self.assertEqual(self.command('worktree', 'list', '--porcelain'), worktrees)
                self.assertFalse((self.root / 'forge').exists())
            service = ForgeSessionGit(self.service)
            before = self.command('status', '--porcelain', '--untracked-files=all')
            with patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('deny'))) as decide:
                with self.assertRaises(ValueError): asyncio.run(service.first_commit(self.project['id'], 'test'))
                self.assertTrue(decide.call_args.kwargs['force_prompt'])
                self.assertIn('Files to commit: 2', decide.call_args.kwargs['description'])
                self.assertIn(str(self.repo), decide.call_args.kwargs['description'])
            self.assertFalse(git.has_commits(self.repo))
            self.assertEqual(self.command('status', '--porcelain', '--untracked-files=all'), before)
            with patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('allow'))):
                asyncio.run(service.first_commit(self.project['id'], 'test'))
            self.assertEqual(self.command('ls-tree', '--name-only', 'HEAD').splitlines(), ['.gitignore', 'space file.txt'])
            with self.assertRaisesRegex(ValueError, 'already has commits'): service.first_commit_target(self.project['id'])
            for mode in ('in_place', 'new_worktree', 'existing_branch'):
                if mode == 'existing_branch': self.command('branch', 'topic')
                session = self.service.create(self.project['id'], 'Task', isolation=mode, branch='topic')
                self.assertEqual(session['forge']['isolation'], mode)

        def test_empty_repository_refused_without_prompt(self):
            with patch.object(permissions, 'decide', new=AsyncMock()) as decide:
                with self.assertRaisesRegex(ValueError, 'no files'):
                    asyncio.run(ForgeSessionGit(self.service).first_commit(self.project['id'], 'test'))
                decide.assert_not_called()

        def test_orphan_head_with_existing_history_cannot_make_first_commit(self):
            (self.repo / 'file').write_text('First file', encoding='utf-8')
            git.initial_commit(self.repo)
            self.command('switch', '--orphan', 'empty')
            (self.repo / 'other').write_text('Other file', encoding='utf-8')
            self.assertFalse(git.has_commits(self.repo))
            with self.assertRaisesRegex(ValueError, 'already has commits'):
                ForgeSessionGit(self.service).first_commit_target(self.project['id'])

    def tearDownModule():
        from core import session_manager_store
        session_manager_store.close()


if __name__ == '__main__': unittest.main()
