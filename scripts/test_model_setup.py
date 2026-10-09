"""Offline model setup checks. No vendor process or external request runs."""
import asyncio
from collections import deque
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from core import codex_cli, model_endpoints, permissions, secret_storage, settings
from core.codex_brain import CodexBrain
from core.middleware import require_admin
from routes import model_setup_routes
from services import model_setup as setup
from services import cookbook_service


def run(awaitable):
    return asyncio.run(awaitable)


class ModelSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='model-setup-', dir=REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'CODEX_HOME': str(self.root / '.codex'), 'CLAUDE_CONFIG_DIR': str(self.root / '.claude')}))
        self.stack.enter_context(patch('pathlib.Path.home', return_value=self.root))
        self.stack.enter_context(patch.object(model_endpoints, 'ENDPOINTS_FILE', str(self.root / 'endpoints.json')))
        self.stack.enter_context(patch.object(settings, 'SETTINGS_FILE', str(self.root / 'settings.json')))
        self.stack.enter_context(patch.object(secret_storage, '_fernet', Fernet(Fernet.generate_key())))
        self.stack.enter_context(patch.object(setup, 'managed_bin', return_value=self.root / 'bin'))
        self.stack.enter_context(patch.object(setup.claude_cli, 'preferred_cli_path', return_value=None))
        self.stack.enter_context(patch.object(setup, '_claude_path', return_value=(None, 'bundled')))
        self.stack.enter_context(patch.object(setup, 'find_codex', return_value=None))
        self.stack.enter_context(patch.object(setup, 'npm_available', return_value=False))
        self.stack.enter_context(patch.object(setup.model_discovery, '_claude_cli_token', return_value=None))
        self.stack.enter_context(patch.object(setup.platform, 'system', return_value='Windows'))
        self.stack.enter_context(patch.object(setup.platform, 'machine', return_value='AMD64'))
        # Any unmocked process call or external HTTP call fails the test.
        self.stack.enter_context(patch.object(subprocess, 'Popen', side_effect=AssertionError('Unmocked process')))
        self.stack.enter_context(patch.object(subprocess, 'run', side_effect=AssertionError('Unmocked process')))
        self.stack.enter_context(patch.object(httpx.AsyncClient, 'send', side_effect=AssertionError('Unmocked HTTP')))

    def auth(self, contents):
        folder = self.root / '.codex'
        folder.mkdir(exist_ok=True)
        (folder / 'auth.json').write_text(contents, encoding='utf-8')

    def test_status_absent(self):
        result = setup.status()
        self.assertFalse(result['claude']['available'])
        self.assertFalse(result['codex']['installed'])
        self.assertFalse(result['codex']['signed_in'])
        self.assertNotIn('default', result)

    def test_status_combinations_and_no_secrets(self):
        self.auth(json.dumps({'tokens': {'access_token': 'CODEX_SECRET', 'id_token': 'ID_SECRET'}}))
        model_endpoints.create_endpoint('Test', 'https://api.openai.com/v1', 'gpt-4o-mini', 'API_SECRET')
        for source in ('bundled', 'separate'):
            with patch.object(setup, '_claude_path', return_value=('claude.exe', source)), \
                 patch.object(setup, 'find_codex', return_value='codex.exe'), \
                 patch.object(setup, 'npm_available', return_value=True), \
                 patch.object(setup.model_discovery, '_claude_cli_token', return_value='CLAUDE_SECRET'), \
                 patch.object(subprocess, 'run', return_value=MagicMock(returncode=0, stdout='codex-cli 0.155.1')):
                result = setup.status()
                self.assertEqual(result['claude']['source'], source)
                self.assertTrue(result['claude']['signed_in'])
                self.assertTrue(result['codex']['signed_in'])
                self.assertEqual(result['codex']['version'], '0.155.1')
                self.assertTrue(result['node']['npm'])
                for secret in ('CODEX_SECRET', 'ID_SECRET', 'CLAUDE_SECRET', 'API_SECRET', 'api_key_encrypted'):
                    self.assertNotIn(secret, json.dumps(result))

    def test_codex_requires_parseable_login(self):
        for contents in ('bad', '[]', '{}', '{"tokens":{}}', '{"auth_mode":"chatgpt"}', '{"tokens":{"access_token":null}}'):
            self.auth(contents)
            self.assertFalse(setup._codex_signed_in(), contents)
        for login in ({'tokens': {'access_token': 'secret'}}, {'tokens': {'id_token': 'secret'}}, {'OPENAI_API_KEY': 'secret'}):
            self.auth(json.dumps(login))
            self.assertTrue(setup._codex_signed_in())

    def test_install_deny_does_nothing(self):
        with patch.object(setup, 'npm_available', return_value=True), \
             patch.object(permissions, 'decide', AsyncMock(return_value=permissions.Decision('deny', 'Cancelled'))) as decide, \
             patch.object(setup, '_npm_install') as install:
            with self.assertRaisesRegex(ValueError, 'Cancelled'):
                run(setup.install_codex())
            install.assert_not_called()
            self.assertTrue(decide.call_args.kwargs['force_prompt'])
            self.assertEqual(decide.call_args.kwargs['target'], 'npm install -g @openai/codex')
            self.assertFalse((self.root / 'bin').exists())

    def test_npm_no_shell_and_filtered_environment(self):
        process = MagicMock()
        process.stdout = io.BytesIO(b'installed\n')
        process.wait.return_value = 0
        with patch.object(setup, 'npm_available', return_value=True), \
             patch.object(permissions, 'decide', AsyncMock(return_value=permissions.Decision('allow', 'Approved'))) as decide, \
             patch.object(setup, 'resolve_command', return_value=['node.exe', 'npm-cli.js', 'install', '-g', '@openai/codex']) as resolve, \
             patch.dict(os.environ, {'JARVIS_TOOL_TOKEN': 'SECRET', 'OPENAI_API_KEY': 'SECRET'}), \
             patch.object(subprocess, 'Popen', return_value=process) as popen:
            self.assertTrue(run(setup.install_codex())['ok'])
            self.assertFalse(popen.call_args.kwargs['shell'])
            self.assertEqual(popen.call_args.args[0][:2], ['node.exe', 'npm-cli.js'])
            self.assertNotIn('OPENAI_API_KEY', popen.call_args.kwargs['env'])
            self.assertNotIn('JARVIS_TOOL_TOKEN', popen.call_args.kwargs['env'])
            decide.assert_awaited_once()
            resolve.assert_called_once()

    def release_install(self, data=b'binary', digest=None, name='codex-x86_64-pc-windows-msvc.exe'):
        asset = {'name': name, 'size': len(data), 'digest': digest if digest is not None else 'sha256:' + hashlib.sha256(data).hexdigest(),
                 'browser_download_url': 'https://github.com/openai/codex/releases/download/v1/' + name}
        calls = []
        def respond(request):
            calls.append(str(request.url))
            if str(request.url) == setup.RELEASE_URL:
                return httpx.Response(200, json={'assets': [asset]})
            return httpx.Response(200, content=data)
        factory = httpx.AsyncClient
        with patch.object(setup.platform, 'system', return_value='Windows'), \
             patch.object(setup.platform, 'machine', return_value='AMD64'), \
             patch.object(permissions, 'decide', AsyncMock(return_value=permissions.Decision('allow', 'Approved'))) as decide, \
             patch.object(setup.httpx, 'AsyncClient', side_effect=lambda **kw: factory(**kw, transport=httpx.MockTransport(respond))), \
             patch.object(factory, 'send', self.real_send):
            result = run(setup.install_codex())
            self.assertEqual([c.kwargs['target'] for c in decide.await_args_list], [setup.RELEASE_URL, asset['browser_download_url']])
            self.assertEqual(len(calls), 2)
            return result

    # Capture before setUp's fail-closed network mock.
    real_send = staticmethod(httpx.AsyncClient.send)

    def test_release_verified_before_write(self):
        self.release_install()
        self.assertEqual((self.root / 'bin' / 'codex.exe').read_bytes(), b'binary')

    def test_digest_mismatch_writes_nothing(self):
        with self.assertRaisesRegex(ValueError, 'SHA-256 verification'):
            self.release_install(digest='sha256:' + '0' * 64)
        self.assertFalse((self.root / 'bin').exists())

    def test_missing_digest_refused(self):
        with self.assertRaisesRegex(ValueError, 'no SHA-256 digest'):
            self.release_install(digest='')
        self.assertFalse((self.root / 'bin').exists())

    def test_archive_traversal_refused_before_any_write(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('codex.exe', b'binary')
            archive.writestr('../escape.exe', b'escape')
        with self.assertRaisesRegex(ValueError, 'Unsafe path'):
            self.release_install(data=stream.getvalue(), name='codex-x86_64-pc-windows-msvc.exe.zip')
        self.assertFalse((self.root / 'bin').exists())
        self.assertFalse((self.root / 'escape.exe').exists())

    def test_archive_links_refused(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            member = tarfile.TarInfo('codex')
            member.type = tarfile.SYMTYPE
            member.linkname = '/outside'
            archive.addfile(member)
        with self.assertRaisesRegex(ValueError, 'Links'):
            setup.executable_bytes(stream.getvalue(), 'codex-x86_64-unknown-linux-musl.tar.gz')

    def test_asset_os_arch_exact_selection(self):
        for system, arch, triple, suffix in [('Windows', 'ARM64', 'aarch64-pc-windows-msvc', '.exe.zip'),
                ('Darwin', 'arm64', 'aarch64-apple-darwin', '.tar.gz'), ('Linux', 'x86_64', 'x86_64-unknown-linux-musl', '.tar.gz')]:
            name = 'codex-' + triple + suffix
            asset = {'name': name, 'digest': 'sha256:' + '1' * 64, 'browser_download_url': 'https://github.com/openai/codex/releases/download/v1/' + name}
            with patch.object(setup.platform, 'system', return_value=system), patch.object(setup.platform, 'machine', return_value=arch):
                self.assertEqual(setup.select_asset({'assets': [{'name': name + '.sig'}, asset]})['name'], name)

    def test_download_rejects_unapproved_hosts_and_caps(self):
        for url in ('http://github.com/openai/codex/releases/download/v1/x', 'https://evil.example/x',
                    'https://github.com/other/codex/releases/download/v1/x', 'https://github.com.evil.example/x',
                    'https://release-assets.githubusercontent.com.evil.example/x'):
            with self.assertRaises(ValueError):
                setup._download_url(url)
        # GitHub's current release CDN (a real Codex asset redirected here, 2026-10-09).
        setup._download_url('https://release-assets.githubusercontent.com/github-production-release-asset/965415649/abc')
        async def probe():
            factory = httpx.AsyncClient
            with patch.object(httpx.AsyncClient, 'send', self.real_send):
                async with factory(transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={'location': 'https://evil.example/binary'}))) as client:
                    with self.assertRaises(ValueError):
                        await setup._download(client, 'https://github.com/openai/codex/releases/download/v1/x', None)
                async with factory(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b'12345'))) as client:
                    with patch.object(setup, 'MAX_DOWNLOAD', 4), self.assertRaises(ValueError):
                        await setup._download(client, 'https://objects.githubusercontent.com/binary', None)
        run(probe())

    def test_sign_in_windows_visible_argv_no_shell(self):
        with patch.object(setup.platform, 'system', return_value='Windows'), \
             patch.object(setup, '_claude_path', return_value=('C:/Program Files/claude.exe', 'bundled')), \
             patch.object(setup, 'find_codex', return_value='C:/Kairos/codex.exe'), \
             patch.object(subprocess, 'Popen') as popen:
            for kind, args in [('claude', ['C:/Program Files/claude.exe']), ('codex', ['C:/Kairos/codex.exe', 'login'])]:
                setup.open_sign_in(kind)
                self.assertEqual(popen.call_args.args[0], args)
                self.assertEqual(popen.call_args.kwargs['creationflags'], subprocess.CREATE_NEW_CONSOLE)
                self.assertFalse(popen.call_args.kwargs['shell'])

    def test_sign_in_other_platforms(self):
        with patch.object(setup, 'find_codex', return_value='/usr/bin/codex'), patch.object(subprocess, 'Popen') as popen:
            with patch.object(setup.platform, 'system', return_value='Darwin'), patch.object(setup, '_mac_launcher', return_value='/data/sign-in.command') as launcher:
                setup.open_sign_in('codex')
                self.assertEqual(popen.call_args.args[0], ['open', '-a', 'Terminal', '/data/sign-in.command'])
                launcher.assert_called_once_with(['/usr/bin/codex', 'login'])
            with patch.object(setup.platform, 'system', return_value='Linux'), patch.object(setup.shutil, 'which', return_value='/usr/bin/xterm'):
                setup.open_sign_in('codex')
                self.assertEqual(popen.call_args.args[0], ['/usr/bin/xterm', '-e', '/usr/bin/codex', 'login'])
            with patch.object(setup.platform, 'system', return_value='Linux'), patch.object(setup.shutil, 'which', return_value=None):
                with self.assertRaisesRegex(ValueError, 'No terminal'):
                    setup.open_sign_in('codex')

    def test_wait_success_and_timeout(self):
        with patch.object(setup, 'status', side_effect=[{'claude': {'signed_in': False}}, {'claude': {'signed_in': True}}]), \
             patch.object(setup.asyncio, 'sleep', AsyncMock()):
            self.assertTrue(run(setup.wait_signed_in('claude', 5))['signed_in'])
        with patch.object(setup, 'status', return_value={'codex': {'signed_in': False}}):
            self.assertTrue(run(setup.wait_signed_in('codex', 0))['timed_out'])

    def test_cli_connection_no_default_and_idempotence(self):
        state = {'claude': {'signed_in': True, 'available': True}, 'codex': {'signed_in': True, 'installed': True}}
        def current():
            return {**state, 'connections': {k: [e for e in model_endpoints.list_endpoints() if e['kind'] == k] for k in ('claude_cli', 'codex_cli')}}
        with patch.object(setup, 'status', side_effect=current):
            result = run(setup.create_connection('claude'))
            claude = result['connection']
            self.assertNotIn('default', result)
            self.assertEqual(claude['model'], '')
            self.assertEqual(run(setup.create_connection('claude'))['connection']['id'], claude['id'])
            run(setup.create_connection('codex'))
            # No default model (David, 2026-08-31): a new chat still starts with none.
            from core.session_manager import SessionManager
            self.assertIsNone(SessionManager().create_session()['model_endpoint_id'])

    def test_api_key_one_small_request_encrypted_and_no_output(self):
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.host == 'api.anthropic.com':
                return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'OUTPUT_SECRET'}]})
            if request.url.host == 'api.openai.com':
                return httpx.Response(200, json={'output': []})
            return httpx.Response(200, json={'choices': [{'message': {'content': 'OUTPUT_SECRET'}}]})
        factory = httpx.AsyncClient
        with patch.object(setup.httpx, 'AsyncClient', side_effect=lambda **kw: factory(**kw, transport=httpx.MockTransport(respond))), \
             patch.object(factory, 'send', self.real_send):
            for provider in setup.PROVIDERS:
                before = len(requests)
                result = run(setup.create_connection('api', provider=provider, key='API_SECRET'))
                self.assertEqual(len(requests), before + 1)
                self.assertNotIn('API_SECRET', json.dumps(result))
                self.assertNotIn('OUTPUT_SECRET', json.dumps(result))
                body = json.loads(requests[-1].content)
                self.assertEqual(body.get('max_tokens', body.get('max_output_tokens')), 16)
        self.assertNotIn('API_SECRET', (self.root / 'endpoints.json').read_text())
        ep = result['connection']
        self.assertEqual(model_endpoints.resolve_runtime(ep['id'])[2], 'API_SECRET')

    def test_api_error_does_not_echo_key_or_body(self):
        response = httpx.Response(401, request=httpx.Request('POST', 'https://api.openai.com/v1/responses'), text='API_SECRET')
        with patch.object(setup.openai_compatible, '_post_chat', AsyncMock(side_effect=httpx.HTTPStatusError('API_SECRET', request=response.request, response=response))):
            result = run(setup.test_api_key('openai', 'API_SECRET'))
            self.assertFalse(result['ok'])
            self.assertNotIn('API_SECRET', json.dumps(result))
            with self.assertRaises(ValueError):
                run(setup.create_connection('api', provider='openai', key='API_SECRET'))
            self.assertEqual(model_endpoints.list_endpoints(), [])

    def test_managed_codex_discovery_shared(self):
        binary = self.root / 'codex.exe'
        binary.write_bytes(b'bin')
        with patch.object(codex_cli, 'managed_bin', return_value=self.root), patch.object(codex_cli.platform, 'system', return_value='Windows'):
            self.assertEqual(codex_cli.find_codex(), str(binary))
            self.assertEqual(CodexBrain._codex_path(), str(binary))

    def test_npm_codex_native_bin_layout(self):
        shim = self.root / 'codex.cmd'
        binary = self.root / 'node_modules' / '@openai' / 'codex-win32-x64' / 'vendor' / 'x86_64-pc-windows-msvc' / 'bin' / 'codex.exe'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'bin')
        with patch.object(codex_cli, 'managed_bin', return_value=self.root / 'managed'), \
             patch.object(codex_cli.shutil, 'which', return_value=str(shim)):
            self.assertEqual(codex_cli.find_codex(), str(binary))

    def test_local_background_connects_after_download(self):
        name = cookbook_service.llamacpp_engine.CATALOG[0]['name']
        with patch.object(cookbook_service.llamacpp_engine, 'list_downloaded', return_value=[]), \
             patch.object(cookbook_service.llamacpp_engine, 'start_download') as download, \
             patch.object(cookbook_service.llamacpp_engine, 'get_download_progress', return_value={'done': True}), \
             patch.object(cookbook_service.llamacpp_engine, 'start', AsyncMock()) as start, \
             patch.object(cookbook_service.llamacpp_engine, 'status', return_value={'running': True, 'model': name, 'base_url': 'http://127.0.0.1:8600/v1'}):
            async def exercise():
                result = cookbook_service.setup_recommended(name)
                self.assertTrue(result['background'])
                await cookbook_service._recommended_tasks[name]
            run(exercise())
            download.assert_called_once_with(name)
            start.assert_awaited_once_with(name)
            progress = cookbook_service.recommended_progress(name)
            self.assertEqual(progress['status'], 'connected')
            self.assertEqual(progress['connection']['kind'], 'local')

    def test_admin_only_every_route(self):
        app = FastAPI()
        app.include_router(model_setup_routes.router)
        def refuse():
            raise HTTPException(403, 'admin privileges required')
        app.dependency_overrides[require_admin] = refuse
        with TestClient(app) as client:
            for method, url, body in [('GET', '/status', None), ('POST', '/codex/install', None),
                ('GET', '/codex/install/job', None), ('POST', '/codex/install/job/cancel', None),
                ('POST', '/sign-in/claude', None), ('GET', '/sign-in/codex/wait', None),
                ('POST', '/api-key/test', {'kind': 'api', 'provider': 'openai', 'key': 'secret'}),
                ('POST', '/connections', {'kind': 'claude'})]:
                response = client.request(method, '/api/model-setup' + url, json=body)
                self.assertEqual(response.status_code, 403, url)

    def test_install_progress_and_real_broker_ask_first(self):
        async def exercise():
            with patch.object(setup, 'npm_available', return_value=True), patch.object(setup, '_npm_install') as install, \
                 patch.object(setup, 'resolve_command', return_value=['node', 'npm-cli.js', 'install', '-g', '@openai/codex']), \
                 patch.object(permissions, '_load', return_value={'rules': [], 'audit': [], 'seeded': []}), patch.object(permissions, '_save'):
                job_id = setup.begin_install()['id']
                for _ in range(20):
                    await asyncio.sleep(0)
                    progress = setup.install_progress(job_id)
                    if progress['permission']:
                        break
                self.assertIsNotNone(progress['permission'])
                install.assert_not_called()
                permissions.answer(progress['permission']['id'], 'once', 'admin')
                await setup._jobs[job_id]['task']
                self.assertEqual(setup.install_progress(job_id)['state'], 'done')
                install.assert_called_once()
        run(exercise())


class ModelSetupBoundaryTests(unittest.TestCase):
    """No temporary home needed for in-memory archive and broker boundaries."""

    def test_zip_validates_all_entries_before_writing(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('codex.exe', b'executable')
            archive.writestr('..\\outside', b'bad')
        with patch.object(setup.platform, 'system', return_value='Windows'), self.assertRaisesRegex(ValueError, 'Unsafe path'):
            setup.executable_bytes(data.getvalue(), 'codex-x86_64-pc-windows-msvc.exe.zip')

    def test_valid_tar_returns_only_executable(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            member = tarfile.TarInfo('codex-aarch64-apple-darwin')
            member.size = 6
            archive.addfile(member, io.BytesIO(b'binary'))
        with patch.object(setup.platform, 'system', return_value='Darwin'):
            self.assertEqual(setup.executable_bytes(data.getvalue(), 'codex-aarch64-apple-darwin.tar.gz'), b'binary')

    def test_windows_prefers_smaller_tar_and_unpacks_it(self):
        # The real release ships codex.exe as .exe.tar.gz (112 MB) and .exe.zip (165 MB).
        stem = 'codex-x86_64-pc-windows-msvc'
        assets = [{'name': stem + suffix, 'digest': 'sha256:' + '1' * 64, 'size': 120 * 1024 * 1024,
                   'browser_download_url': 'https://github.com/openai/codex/releases/download/v1/' + stem + suffix}
                  for suffix in ('.exe.zip', '.exe.tar.gz')]
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            member = tarfile.TarInfo(stem + '.exe')
            member.size = 6
            archive.addfile(member, io.BytesIO(b'binary'))
        with patch.object(setup.platform, 'system', return_value='Windows'), patch.object(setup.platform, 'machine', return_value='AMD64'):
            self.assertEqual(setup.select_asset({'assets': assets})['name'], stem + '.exe.tar.gz')
            self.assertEqual(setup.executable_bytes(data.getvalue(), stem + '.exe.tar.gz'), b'binary')

    def test_release_digest_failures_never_write(self):
        factory = httpx.AsyncClient
        for digest, expected in [('', 'no SHA-256 digest'), ('sha256:' + '0' * 64, 'SHA-256 verification')]:
            asset = {'name': 'codex-x86_64-pc-windows-msvc.exe', 'digest': digest,
                     'browser_download_url': 'https://github.com/openai/codex/releases/download/v1/codex.exe'}
            def respond(request):
                return httpx.Response(200, json={'assets': [asset]}) if str(request.url) == setup.RELEASE_URL else httpx.Response(200, content=b'binary')
            with patch.object(setup, 'npm_available', return_value=False), \
                 patch.object(setup.platform, 'system', return_value='Windows'), patch.object(setup.platform, 'machine', return_value='AMD64'), \
                 patch.object(setup, '_ask', AsyncMock()), patch.object(setup, '_write_binary') as write, \
                 patch.object(setup.httpx, 'AsyncClient', side_effect=lambda **kw: factory(**kw, transport=httpx.MockTransport(respond))):
                with self.assertRaisesRegex(ValueError, expected):
                    run(setup.install_codex())
                write.assert_not_called()

    def test_denied_install_never_launches_or_downloads(self):
        with patch.object(setup, 'npm_available', return_value=True), \
             patch.object(permissions, 'decide', AsyncMock(return_value=permissions.Decision('deny', 'Cancelled'))) as decide, \
             patch.object(setup, '_npm_install') as npm, patch.object(setup.httpx, 'AsyncClient') as http:
            with self.assertRaisesRegex(ValueError, 'Cancelled'):
                run(setup.install_codex())
            self.assertTrue(decide.call_args.kwargs['force_prompt'])
            npm.assert_not_called()
            http.assert_not_called()

    def test_admin_dependency_is_on_every_setup_route(self):
        for route in model_setup_routes.router.routes:
            self.assertIn(require_admin, [d.call for d in route.dependant.dependencies], route.path)

    def test_install_cancel_before_approval_is_effective(self):
        async def exercise():
            with patch.object(setup, 'npm_available', return_value=True), patch.object(permissions, 'decide', AsyncMock()) as decide:
                job = {'cancel_requested': True, 'lines': deque(maxlen=100)}
                with self.assertRaisesRegex(ValueError, 'cancelled'):
                    await setup.install_codex(job=job)
                decide.assert_not_awaited()
        run(exercise())

    def test_polled_install_uses_the_permission_broker(self):
        async def exercise():
            with patch.object(setup, 'npm_available', return_value=True), patch.object(setup, '_npm_install') as install, \
                 patch.object(setup, 'resolve_command', return_value=['node', 'npm-cli.js', 'install', '-g', '@openai/codex']), \
                 patch.object(permissions, '_load', return_value={'rules': [], 'audit': [], 'seeded': []}), patch.object(permissions, '_save'):
                job_id = setup.begin_install()['id']
                for _ in range(20):
                    await asyncio.sleep(0)
                    progress = setup.install_progress(job_id)
                    if progress['permission']:
                        break
                self.assertIsNotNone(progress['permission'])
                install.assert_not_called()
                permissions.answer(progress['permission']['id'], 'once', 'admin')
                await asyncio.wait_for(setup._jobs[job_id]['task'], timeout=2)
                self.assertEqual(setup.install_progress(job_id)['state'], 'done')
                install.assert_called_once()
        run(exercise())


if __name__ == '__main__':
    unittest.main()
