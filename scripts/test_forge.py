"""Forge Phase 1 contracts, deterministic real git fixtures, no network."""
import asyncio
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
environment = tempfile.TemporaryDirectory(prefix='kairos-forge-tests-')
os.environ['JARVIS_DATA_DIR'] = str(Path(environment.name) / 'app-data')

import httpx
from fastapi import FastAPI
from core import middleware, session_manager_store
from services import forge_git, forge_projects as projects_module
from routes import forge_routes

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def tearDownModule():
    session_manager_store.close()
    environment.cleanup()


class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.projects = projects_module.ForgeProjects(self.root / 'projects.json')
        forge_git._cache.clear()

    def git(self, *args, author='Alice', date='2025-01-01T12:00:00+00:00', cwd=None):
        env = forge_git.git_env()
        env.update(GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=author.lower() + '@fixture.test',
                   GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL=author.lower() + '@fixture.test',
                   GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
                                cwd=cwd or self.repo, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def fixture(self):
        self.git('init', '-b', 'main')
        (self.repo / 'README.md').write_text('Garden\nReady\n', encoding='utf-8', newline='')
        (self.repo / 'code.py').write_text('one\ntwo\n', encoding='utf-8', newline='')
        self.git('add', '.'); self.git('commit', '-m', 'Seed')
        (self.repo / 'code.py').write_text('one\nthree\n', encoding='utf-8', newline='')
        (self.repo / 'notes.txt').write_text('note\n', encoding='utf-8', newline='')
        self.git('add', '.'); self.git('commit', '-m', 'Previous window', author='Bob', date='2026-09-01T12:00:00+00:00')
        (self.repo / 'code.py').write_text('one\nthree\nfour\n', encoding='utf-8', newline='')
        (self.repo / 'app.js').write_text('a\nb\n', encoding='utf-8', newline='')
        self.git('add', '.'); self.git('commit', '-m', 'Current window', date='2026-10-01T12:00:00+00:00')
        (self.repo / 'notes.txt').write_text('', encoding='utf-8', newline='')
        (self.repo / 'README.md').write_text('Garden\nReady\nDone\n', encoding='utf-8', newline='')
        self.git('add', '.'); self.git('commit', '-m', 'Finish', author='Bob', date='2026-10-08T12:00:00+00:00')
        return self.projects.add(str(self.repo), 'Garden')

    def test_project_round_trip_remove_keeps_files(self):
        p = self.projects.add(str(self.repo), 'Garden')
        self.assertEqual(self.projects.add(str(self.repo))['id'], p['id'])
        reloaded = projects_module.ForgeProjects(self.projects.file)
        self.assertEqual(reloaded.get(p['id'])['path'], str(self.repo.resolve()))
        self.assertIsNone(p['last_opened_at'])
        self.assertIsNotNone(reloaded.opened(p['id'])['last_opened_at'])
        reloaded.remove(p['id'])
        self.assertEqual(reloaded.list(), [])
        self.assertTrue(self.repo.is_dir())

    def test_path_vetting_and_app_folders(self):
        self.assertEqual(projects_module.vet_project(str(self.repo)), str(self.repo.resolve()))
        sensitive = self.root / '.ssh'; sensitive.mkdir()
        for path in ('', str(self.root.anchor), str(sensitive), str(ROOT), str(ROOT / 'static'), str(ROOT / 'data'), str(self.root / 'missing')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                projects_module.vet_project(path)

    def test_new_init_and_root_setting(self):
        destination = self.root / 'projects'
        with patch.object(projects_module, 'get_setting', return_value=str(destination)):
            self.assertFalse(destination.exists())
            self.assertEqual(projects_module.forge_root(), destination)
            project = self.projects.new('My garden / idea')
            folder = Path(project['path'])
            self.assertEqual(folder.parent, destination)
            self.assertEqual(folder.name, 'My-garden-idea')
            self.assertTrue((folder / '.git').is_dir())
            self.assertIn('My garden / idea', (folder / 'README.md').read_text())
            self.assertIn('.env', (folder / '.gitignore').read_text())
            with self.assertRaises(FileExistsError): self.projects.new('My garden / idea')
            result = forge_git.summary(project, now=NOW)
            self.assertEqual(result['state'], 'empty')
            self.assertEqual(result['lifespan']['commits'], 0)
        with patch.object(projects_module, 'update_settings') as save:
            self.assertEqual(projects_module.set_root(str(destination)), str(destination))
            save.assert_called_once_with(forge_root=str(destination))
        for name in ('../', '---', 'CON', 'LPT1'):
            with self.assertRaises(ValueError): projects_module.folder_name(name)

    def test_clone_validator_and_local_bare_happy_path(self):
        for url in ('https://example.test/owner/repo.git', 'git@example.test:owner/repo.git'):
            self.assertEqual(projects_module.validate_clone_url(url), url)
        for url in ('file:///tmp/a', 'ext::sh -c boom', '--upload-pack=boom', '-x', str(self.repo), 'C:\\repo', '../repo', 'ssh://host/repo', 'https://', 'https://host/', 'git@host:-x/repo', 'git@host:owner/../repo', 'https://user:secret@host/repo'):
            with self.subTest(url=url), self.assertRaises(ValueError): projects_module.validate_clone_url(url)
        self.fixture()
        bare = self.root / 'bare.git'
        self.git('clone', '--bare', '--', str(self.repo), str(bare), cwd=self.root)
        destination = self.root / 'cloned'
        forge_git.clone_repo(self.root, str(bare), destination)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=destination), self.git('rev-parse', 'HEAD'))
        with self.assertRaises(forge_git.GitError): forge_git.clone_repo(self.root, str(bare), destination)
        with self.assertRaises(forge_git.GitError): forge_git.clone_repo(self.root, str(bare), self.root.parent / 'escape')
        # A failed clone removes the folder it reserved, so the same name can be retried.
        failed = self.root / 'retry'
        with self.assertRaises(forge_git.GitError): forge_git.clone_repo(self.root, str(self.root / 'missing.git'), failed)
        self.assertFalse(failed.exists())
        forge_git.clone_repo(self.root, str(bare), failed)
        self.assertTrue((failed / '.git').is_dir())

    def test_all_summary_numbers(self):
        p = self.fixture()
        summary = forge_git.summary(p, now=NOW)
        life = summary['lifespan']
        self.assertEqual(summary['state'], 'ok')
        self.assertEqual(summary['branch'], 'main')
        self.assertEqual((life['commits'], life['contributors'], life['added'], life['removed']), (4, 2, 10, 2))
        self.assertEqual(life['first_commit'], datetime(2025, 1, 1, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(life['last_commit'], datetime(2026, 10, 8, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(life['delta'], dict(commits=1, contributors=1, added=2, removed=0, age_days=30))
        self.assertEqual(life['bucket'], 'monthly')
        self.assertEqual(sum(b['commits'] for b in life['buckets']), 4)
        self.assertEqual(len(life['buckets']), 22)
        self.assertEqual(len(summary['activity']), 14)
        self.assertEqual(tuple(sum(d[k] for d in summary['activity']) for k in ('commits', 'added', 'removed')), (2, 4, 1))
        languages = {row['name']: row['bytes'] for row in summary['languages']}
        self.assertEqual(languages, {'.md': 18, '.py': 15, '.js': 4, '.txt': 0})
        self.assertEqual(sum(map(sum, summary['rhythm'])), 4)
        expected = [[0] * 24 for _ in range(7)]
        for stamp in ('2025-01-01T12:00:00+00:00', '2026-09-01T12:00:00+00:00', '2026-10-01T12:00:00+00:00', '2026-10-08T12:00:00+00:00'):
            local = datetime.fromisoformat(stamp).astimezone()
            expected[local.weekday()][local.hour] += 1
        self.assertEqual(summary['rhythm'], expected)
        self.assertEqual(summary['hotspots'][0], dict(path='code.py', commits=3, added=4, removed=1))
        self.assertEqual(summary['contributors'], [dict(name='Alice', commits=2, added=7, removed=0), dict(name='Bob', commits=2, added=3, removed=2)])
        self.assertEqual(summary['last_commit']['subject'], 'Finish')

    def test_weekly_languages_other_and_cache_head_index(self):
        p = self.fixture()
        first = forge_git.summary(p, now=NOW)
        with patch.object(forge_git, '_summarize', wraps=forge_git._summarize) as calculate:
            second = forge_git.summary(p, now=NOW)
            self.assertEqual(first, second)
            calculate.assert_not_called()
            second['lifespan']['commits'] = 999
            self.assertEqual(forge_git.summary(p, now=NOW)['lifespan']['commits'], 4)
            (self.repo / 'new.go').write_text('hello\n', encoding='utf-8', newline='')
            self.git('add', '.')
            forge_git.summary(p, now=NOW)
            self.assertEqual(calculate.call_count, 1)
            self.git('commit', '-m', 'Head moved', date='2026-10-09T10:00:00+00:00')
            after = forge_git.summary(p, now=NOW)
            self.assertNotEqual(first['head'], after['head'])
            self.assertEqual(after['lifespan']['commits'], 5)
        for extension in ('rs', 'rb', 'java', 'css', 'html', 'c'):
            (self.repo / ('file.' + extension)).write_text('abc\n', encoding='utf-8', newline='')
        self.git('add', '.')
        languages = forge_git.summary(p, now=NOW)['languages']
        self.assertEqual(len(languages), 7)
        self.assertEqual(languages[-1]['name'], 'Other')
        recent = forge_git._summarize(str(self.repo), [dict(sha='x', at=NOW.timestamp() - 86400, name='A', email='a', subject='A', files=[])], NOW)
        self.assertEqual(recent['lifespan']['bucket'], 'weekly')
        self.assertEqual(sum(b['commits'] for b in recent['lifespan']['buckets']), 1)

    def test_non_git_and_read_only_runner(self):
        p = self.projects.add(str(self.repo))
        self.assertEqual(forge_git.summary(p)['state'], 'not_git')
        with self.assertRaises(forge_git.GitError): forge_git.run_git(self.repo, 'push')
        env = forge_git.git_env()
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env['GIT_ASKPASS'], os.devnull)
        self.assertEqual(env['GCM_INTERACTIVE'], 'Never')

    def test_timeouts_and_output_cap(self):
        self.fixture()
        with self.assertRaisesRegex(forge_git.GitError, 'limit'):
            forge_git.run_git(self.repo, 'files', output_cap=2)
        process = MagicMock()
        process.stdout = io.BytesIO(b'')
        process.wait.side_effect = [subprocess.TimeoutExpired('git', 1), 0]
        process.__enter__.return_value = process
        with patch.object(forge_git.subprocess, 'Popen', return_value=process) as popen:
            with self.assertRaisesRegex(forge_git.GitError, 'timed out'):
                forge_git.run_git(self.repo, 'head', timeout=1)
            process.kill.assert_called_once()
            kwargs = popen.call_args.kwargs
            self.assertNotIn('shell', kwargs)
            self.assertEqual(kwargs['cwd'], str(self.repo.resolve()))

    def test_admin_only_routes_and_home_activity(self):
        p = self.fixture()
        app = FastAPI(); app.include_router(forge_routes.router)
        async def requests():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                with patch.object(middleware, 'require_user', return_value='member'), patch.object(middleware.auth_manager, 'is_admin', return_value=False):
                    for route in forge_routes.router.routes:
                        url = route.path.replace('{project_id}', p['id'])
                        response = await client.request(next(iter(route.methods)), url, json={})
                        self.assertEqual(response.status_code, 403, url)
                app.dependency_overrides[middleware.require_admin] = lambda: 'admin'
                with patch.object(forge_routes, 'forge_projects', self.projects):
                    self.assertEqual((await client.get('/api/forge/projects')).status_code, 200)
                    response = await client.get('/api/forge/projects/' + p['id'] + '/summary')
                    self.assertEqual(response.json()['lifespan']['commits'], 4)
                    with patch('core.session_manager.session_manager.list_sessions', return_value=[{'id': 'chat', 'title': 'Build'}]), patch('core.session_manager.session_manager.get_session', return_value={'workspace_dir': p['path']}), patch('services.chat_service.is_busy', return_value=True):
                        response = await client.get('/api/forge/activity')
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()['working'][0]['project_name'], 'Garden')
        asyncio.run(requests())


if __name__ == '__main__':
    unittest.main()
