"""Git contracts without processes. --integration enables real local repository fixtures."""
import asyncio
import os
import sys
import threading
from contextlib import ExitStack
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['JARVIS_DATA_DIR'] = str(ROOT / 'data' / 'forge3-unit-tests')
from services import forge_git as git
from services.forge_session_git import ForgeSessionGit
from core import permissions


@asynccontextmanager
async def operation(_id):
    yield


def chat_stub():
    # `from services import chat_service` reads the package attribute first; once the real
    # module is imported (the --integration run), a sys.modules stub alone is bypassed.
    import services
    stub = SimpleNamespace(session_operation=operation)
    stack = ExitStack()
    stack.enter_context(patch.dict(sys.modules, {'services.chat_service': stub}))
    stack.enter_context(patch.object(services, 'chat_service', stub, create=True))
    return stack


class GitTests(unittest.TestCase):
    def test_status_rename_and_untracked(self):
        rows = git.status_files(' M space file\0R  new\0old\0?? fresh\0')
        self.assertFalse(rows[0]['staged']); self.assertTrue(rows[0]['unstaged'])
        self.assertEqual(rows[1]['original'], 'old'); self.assertTrue(rows[1]['staged'])
        self.assertTrue(rows[2]['unstaged']); self.assertFalse(rows[2]['staged'])

    def test_stage_path_separator_and_commit_hooks(self):
        with patch.object(git, '_run', return_value=(0, '')) as runner:
            git.stage(ROOT, 'file'); self.assertEqual(runner.call_args.args[1], ['add', '-A', '--', 'file'])
            git.stage(ROOT, unstage=True); self.assertEqual(runner.call_args.args[1], ['restore', '--staged', '--', '.'])
        with patch.object(git, 'session_status', return_value={'files': [{'staged': False}]}), patch.object(git, 'stage') as stage, patch.object(git, '_run') as runner:
            git.commit(ROOT, 'Message')
            stage.assert_called_once_with(ROOT)
            self.assertTrue(runner.call_args.kwargs['hooks'])
            self.assertEqual(runner.call_args.args[1], ['commit', '-m', 'Message'])

    def test_dirty_branch_refused_and_pull_ff_only(self):
        with patch.object(git, 'validate_branch'), patch.object(git, 'run_git', return_value=(0, ' M file\0')), patch.object(git, '_run') as runner:
            with self.assertRaisesRegex(ValueError, 'before switching'): git.switch_branch(ROOT, 'topic')
            runner.assert_not_called()
        with patch.object(git, 'run_git', return_value=(0, '')), patch.object(git, 'session_status', return_value={'upstream': 'origin/topic'}), patch.object(git, '_run') as runner:
            git.pull(ROOT)
            self.assertEqual(runner.call_args.args[1], ['pull', '--ff-only'])

    def test_push_never_forces(self):
        with patch.object(git, '_run') as runner:
            git.push(ROOT, dict(remote='origin', branch='topic', destination='review'))
            args = runner.call_args.args[1]
            self.assertEqual(args, ['push', '--set-upstream', '--', 'origin', 'refs/heads/topic:refs/heads/review'])
            self.assertNotIn('--force', args)

    def test_merge_conflict_aborted_and_dirty_main_refused(self):
        merge_heads = iter([1, 0])
        def runner(_root, args, **kwargs):
            if args == ['rev-parse', '--verify', 'MERGE_HEAD']: return next(merge_heads), ''
            if args[:2] == ['merge', '--no-edit']: raise git.GitError('conflict')
            if args[:2] == ['diff', '--name-only']: return 0, 'one\0two\0'
            return 0, ''
        with patch.object(git, 'validate_branch'), patch.object(git, 'run_git', side_effect=[(0, 'main'), (0, '')]), patch.object(git, '_run', side_effect=runner) as run:
            with self.assertRaisesRegex(ValueError, 'one, two'): git.merge_back(ROOT, 'topic', 'main')
            self.assertIn(['merge', '--abort'], [c.args[1] for c in run.call_args_list])
        with patch.object(git, 'validate_branch'), patch.object(git, 'run_git', side_effect=[(0, 'main'), (0, ' M dirty')]), patch.object(git, '_run') as run:
            with self.assertRaisesRegex(ValueError, 'uncommitted'): git.merge_back(ROOT, 'topic', 'main')
            run.assert_not_called()

    def test_push_approval_is_fresh_exact_and_denial_does_not_push(self):
        sessions = Mock(); sessions.lock = threading.RLock(); sessions.workspace.return_value = ROOT
        service = ForgeSessionGit(sessions); service.status = Mock(return_value={})
        target = dict(remote='origin', url='local-bare', branch='topic', destination='review', head='abc')
        for behavior in ['deny', 'allow']:
            with chat_stub(), patch.object(git, 'push_target', return_value=target), patch.object(git, 'push') as push, patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision(behavior))) as decide:
                if behavior == 'deny':
                    with self.assertRaises(ValueError): asyncio.run(service.approved('s', 'push', 'forge-git:s'))
                    push.assert_not_called()
                else:
                    asyncio.run(service.approved('s', 'push', 'forge-git:s')); push.assert_called_once()
                self.assertTrue(decide.call_args.kwargs['force_prompt'])
                description = decide.call_args.kwargs['description']
                for value in ['origin', 'local-bare', 'topic', 'review']: self.assertIn(value, description)

    def test_end_requires_discard_confirmation_for_unmerged_and_keeps_branch(self):
        sessions = Mock(); service = ForgeSessionGit(sessions)
        sessions.get.return_value = {'forge': {'project_id': 'p', 'base_branch': 'main', 'isolation': 'new_worktree'}}
        sessions.project.return_value = ({}, ROOT)
        sessions.remove.return_value = {}
        service.status = Mock(return_value={'files': [], 'unmerged': True, 'branch': 'topic'})
        with self.assertRaises(ValueError): service.end('s', 'discard')
        sessions.remove.assert_not_called()
        with patch.object(git, 'discard_branch', return_value=True) as delete:
            service.end('s', 'discard', True); sessions.remove.assert_called_once_with('s', True, True)
            delete.assert_called_once_with(ROOT, 'topic', 'main')
        service.end('s', 'keep-branch'); self.assertEqual(sessions.remove.call_args.args, ('s', False, False))

    def test_all_routes_admin_and_git_not_agent_tools(self):
        source = (ROOT / 'routes/forge_routes.py').read_text(encoding='utf-8')
        self.assertIn('dependencies=[Depends(require_admin)]', source)
        for verb in ['get', 'post']: self.assertIn(f"@router.{verb}('/sessions/{{session_id}}/git')", source)
        tools = (ROOT / 'core/tool_registry.py').read_text(encoding='utf-8')
        self.assertNotIn('forge_git_push', tools)

    def test_merge_approval_shows_both_branches_and_stale_push_does_not_run(self):
        sessions = Mock(); sessions.lock = threading.RLock(); sessions.workspace.return_value = ROOT
        service = ForgeSessionGit(sessions)
        service.merge_target = Mock(return_value=dict(branch='topic', base='main', repo=str(ROOT)))
        with chat_stub(), patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('deny'))) as decide, patch.object(git, 'merge_back') as merge:
            with self.assertRaises(ValueError): asyncio.run(service.approved('s', 'merge', 'forge-git:s'))
            self.assertIn('topic', decide.call_args.kwargs['description'])
            self.assertIn('main', decide.call_args.kwargs['description'])
            self.assertTrue(decide.call_args.kwargs['force_prompt'])
            merge.assert_not_called()
        target = dict(remote='origin', url='bare', branch='topic', destination='topic', head='first')
        with chat_stub(), patch.object(git, 'push_target', side_effect=[target, {**target, 'head': 'second'}]), patch.object(permissions, 'decide', new=AsyncMock(return_value=permissions.Decision('allow'))), patch.object(git, 'push') as push:
            with self.assertRaisesRegex(ValueError, 'changed during approval'): asyncio.run(service.approved('s', 'push', 'forge-git:s'))
            push.assert_not_called()

    def test_discard_never_deletes_main_checkout_or_base_branch(self):
        with patch.object(git, 'validate_branch'), patch.object(git, 'run_git', return_value=(0, 'main')), patch.object(git, '_run') as run:
            self.assertFalse(git.discard_branch(ROOT, 'main', 'main'))
            run.assert_not_called()


if '--integration' in sys.argv:
    sys.argv.remove('--integration')
    from test_forge_sessions import ForgeSessionTests
    class GitFixtureTests(ForgeSessionTests):
        def test_stage_commit_switch_merge_real_repository(self):
            session = self.create(); sid = session['id']; root = Path(session['workspace_dir'])
            self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.test')
            self.git('config', 'commit.gpgsign', 'false')
            service = ForgeSessionGit(self.service)
            self.write(root / 'code.txt', 'Human edit\n')
            service.action(sid, 'stage', path='code.txt')
            self.assertTrue(service.status(sid)['files'][0]['staged'])
            service.action(sid, 'unstage', path='code.txt')
            with self.assertRaises(ValueError): service.action(sid, 'switch', branch='main')
            service.action(sid, 'commit', message='Human edit')
            self.assertFalse(service.status(sid)['files'])
            git.merge_back(self.repo, session['forge']['branch'], 'main')
            self.assertEqual((self.repo / 'code.txt').read_text(), 'Human edit\n')
            service.end(sid, 'keep-branch')
            self.assertFalse(root.exists())
        def test_local_bare_push_and_ff_only(self):
            session = self.create(); root = Path(session['workspace_dir'])
            bare = self.root / 'bare'; self.git('init', '--bare', str(bare))
            self.git('remote', 'add', 'origin', str(bare))
            git.push(root, git.push_target(root))
            self.assertIsNotNone(git.session_status(root)['upstream'])
            git.pull(root)
        def test_conflict_abort_real(self):
            session = self.create(); root = Path(session['workspace_dir'])
            self.write(root / 'code.txt', 'topic\n'); self.git('add', '.', cwd=root); self.git('commit', '-m', 'Topic', cwd=root)
            self.write(self.repo / 'code.txt', 'base\n'); self.git('add', '.'); self.git('commit', '-m', 'Base')
            with self.assertRaisesRegex(ValueError, 'Conflicted files'): git.merge_back(self.repo, session['forge']['branch'], 'main')
            self.assertEqual(self.git('status', '--porcelain'), '')
            self.assertEqual((self.repo / 'code.txt').read_text(), 'base\n')

if __name__ == '__main__': unittest.main()
