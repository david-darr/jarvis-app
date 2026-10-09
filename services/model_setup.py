"""Guided model setup. Vendor logins stay in vendor-owned consoles/files.

All network and process boundaries are injectable through ordinary mocks.
Claude starts interactively with no arguments: its bundled first-run flow
offers sign-in. Already authenticated users can enter /login in that window.
"""
import asyncio
from collections import deque
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import zipfile

import httpx
import claude_agent_sdk

from core import claude_cli, model_discovery, model_endpoints, permissions
from core.codex_cli import find_codex, managed_bin
from core.providers import openai_compatible
from services.forge_apps import child_env, command_args, resolve_command, CHOICES

RELEASE_URL = 'https://api.github.com/repos/openai/codex/releases/latest'
# Sized from the real release (2026-10-09): Windows .exe.tar.gz 112 MB, .exe.zip
# 165 MB, codex.exe 335 MB unpacked; Linux and macOS archives about 100-112 MB.
MAX_DOWNLOAD = 300 * 1024 * 1024
MAX_EXPANDED = 600 * 1024 * 1024
PROVIDERS = {
    'openai': ('OpenAI', 'https://api.openai.com/v1', 'gpt-4o-mini'),
    'anthropic': ('Anthropic', 'https://api.anthropic.com', 'claude-haiku-4-5'),
    'openrouter': ('OpenRouter', 'https://openrouter.ai/api/v1', 'openai/gpt-4o-mini'),
    'google': ('Google Gemini', 'https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-2.5-flash'),
}
_jobs = {}


def _claude_path():
    separate = claude_cli.preferred_cli_path()
    if separate:
        return separate, 'separate'
    bundled = Path(claude_agent_sdk.__file__).parent / '_bundled' / ('claude.exe' if platform.system() == 'Windows' else 'claude')
    return (str(bundled), 'bundled') if bundled.is_file() else (None, 'bundled')


def _codex_signed_in():
    path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'auth.json'
    try:
        if path.stat().st_size > 1024 * 1024:
            return False
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return False
        tokens = data.get('tokens')
        return (isinstance(data.get('OPENAI_API_KEY'), str) and bool(data['OPENAI_API_KEY'].strip())) or (
            isinstance(tokens, dict) and any(isinstance(tokens.get(k), str) and tokens[k].strip()
                                            for k in ('access_token', 'id_token')))
    except (OSError, ValueError):
        return False


def npm_available():
    try:
        resolve_command(command_args('npm --version'), child_env(0))
        return True
    except (OSError, ValueError):
        return False


def status():
    claude, source = _claude_path()
    codex = find_codex()
    version = None
    if codex:
        try:
            result = subprocess.run([codex, '--version'], capture_output=True, text=True, shell=False,
                                    stdin=subprocess.DEVNULL, timeout=5,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if platform.system() == 'Windows' else 0)
            match = re.search(r'\b\d+\.\d+\.\d+(?:[-+][\w.-]+)?\b', result.stdout or '')
            if result.returncode == 0 and match:
                version = match.group(0)
        except (OSError, subprocess.SubprocessError):
            pass
    connections = {kind: [] for kind in ('claude_cli', 'codex_cli', 'api', 'local')}
    for ep in model_endpoints.list_endpoints():
        connections.setdefault(ep['kind'], []).append({k: ep[k] for k in ('id', 'name', 'kind', 'model')})
    return {'claude': {'available': bool(claude), 'source': source,
                       'signed_in': bool(model_discovery._claude_cli_token())},
            'codex': {'installed': bool(codex), 'version': version, 'path': codex, 'signed_in': _codex_signed_in()},
            'node': {'npm': npm_available()}, 'connections': connections}


def _line(job, text):
    if job is not None:
        job['lines'].append(str(text)[:2000])


async def _ask(surface, exact, job=None):
    if job and job.get('cancel_requested'):
        raise ValueError('Installation cancelled.')
    decision = await permissions.decide(surface=surface, tool='model_setup_install',
        arguments={'command_or_url': exact}, title='Install Codex?', description=f'Kairos will run or download:\n{exact}',
        target=exact, choices=CHOICES, is_admin=True, force_prompt=True)
    if decision.behavior != 'allow':
        raise ValueError(decision.reason or 'Installation cancelled.')
    if job and job.get('cancel_requested'):
        raise ValueError('Installation cancelled.')


def select_asset(release):
    system = platform.system()
    machine = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(machine)
    target = {'Windows': 'pc-windows-msvc', 'Darwin': 'apple-darwin', 'Linux': 'unknown-linux-musl'}.get(system)
    if not arch or not target:
        raise ValueError('There is no supported Codex download for this computer.')
    stem = f'codex-{arch}-{target}'
    # Exact target names only; exclude signatures, checksums, SDKs and wrappers.
    names = [stem + suffix for suffix in (('.exe.tar.gz', '.exe.zip', '.zip', '.exe') if system == 'Windows' else ('.tar.gz', '.zip', ''))]
    assets = release.get('assets', [])
    asset = next((a for name in names for a in assets if a.get('name') == name), None)
    if not asset:
        raise ValueError('The official release has no Codex binary for this computer.')
    if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', asset.get('digest') or ''):
        raise ValueError('The official download has no SHA-256 digest. Nothing was installed.')
    _download_url(asset.get('browser_download_url', ''))
    if asset.get('size', 0) > MAX_DOWNLOAD:
        raise ValueError('The Codex download exceeds the size limit.')
    return asset


# GitHub redirects release downloads to its asset CDN: release-assets is the
# current host (checked live 2026-10-09), objects the older one. The SHA-256
# from the release metadata, checked after download, is the real guarantee.
DOWNLOAD_HOSTS = frozenset({'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'})


def _download_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in DOWNLOAD_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Codex downloads must use HTTPS on GitHub's release hosts.")
    if parsed.hostname == 'github.com' and not parsed.path.startswith('/openai/codex/releases/download/'):
        raise ValueError('This is not an official OpenAI Codex release URL.')


async def _download(client, url, job):
    for _ in range(6):
        _download_url(url)
        async with client.stream('GET', url) as response:
            if response.is_redirect:
                url = str(response.url.join(response.headers['location']))
                continue
            response.raise_for_status()
            if int(response.headers.get('content-length', 0)) > MAX_DOWNLOAD:
                raise ValueError('The Codex download exceeds the size limit.')
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_DOWNLOAD:
                    raise ValueError('The Codex download exceeds the size limit.')
                data.extend(chunk)
                _line(job, f'Downloaded {len(data) // 1024} KB')
            return bytes(data)
    raise ValueError('The Codex download redirected too many times.')


def _safe_name(name):
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or ':' in name or not path.parts:
        raise ValueError('Unsafe path in the Codex archive. Nothing was installed.')


def executable_bytes(data, name):
    """Validate the ENTIRE archive before writing any file; never extract links."""
    windows = platform.system() == 'Windows'
    wanted = {'codex.exe', name.removesuffix('.zip').removesuffix('.tar.gz')} if windows else {'codex', name.removesuffix('.tar.gz').removesuffix('.zip')}
    found = []
    if name.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 1000 or sum(m.file_size for m in members) > MAX_EXPANDED:
                raise ValueError('The Codex archive is too large.')
            for member in members:
                _safe_name(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError('Links or special files in the Codex archive are not allowed.')
                if not member.is_dir() and PurePosixPath(member.filename).name in wanted:
                    found.append(archive.read(member))
    elif name.endswith('.tar.gz'):
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            total = 0
            for count, member in enumerate(archive):
                total += member.size
                if count >= 1000 or total > MAX_EXPANDED:
                    raise ValueError('The Codex archive is too large.')
                _safe_name(member.name)
                if not (member.isfile() or member.isdir()):
                    raise ValueError('Links or special files in the Codex archive are not allowed.')
                if member.isfile() and PurePosixPath(member.name).name in wanted:
                    found.append(archive.extractfile(member).read())
    else:
        return data
    if len(found) != 1 or not found[0]:
        raise ValueError('The Codex archive must contain exactly one executable.')
    return found[0]


def _write_binary(data):
    folder = managed_bin()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ('codex.exe' if platform.system() == 'Windows' else 'codex')
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            pending = Path(stream.name)
            stream.write(data)
        if platform.system() != 'Windows':
            pending.chmod(0o755)
        os.replace(pending, target)
    finally:
        if pending and pending.exists():
            pending.unlink()
    return str(target)


def _npm_install(args, env, job):
    process = subprocess.Popen(args, env=env, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if platform.system() == 'Windows' else 0)
    # A reader keeps output bounded while a timeout also covers a silent npm.
    import threading
    def read():
        with process.stdout:
            while chunk := process.stdout.read(1024):
                _line(job, chunk.decode('utf-8', errors='replace').strip())
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=300)
        reader.join(timeout=2)
        if code:
            raise ValueError('Codex installation failed. Check the progress above and try again.')
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise ValueError('Codex installation took too long. Try again.')


async def install_codex(surface='model-setup', job=None):
    if npm_available():
        exact = 'npm install -g @openai/codex'
        await _ask(surface, exact, job)
        env = child_env(0)
        args = resolve_command(command_args(exact), env)
        _line(job, exact)
        await asyncio.to_thread(_npm_install, args, env, job)
    else:
        await _ask(surface, RELEASE_URL, job)
        _line(job, 'Finding the official release for this computer.')
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(RELEASE_URL, headers={'Accept': 'application/vnd.github+json'})
            response.raise_for_status()
            asset = select_asset(response.json())
            url = asset['browser_download_url']
            await _ask(surface, url, job)
            data = await asyncio.wait_for(_download(client, url, job), timeout=120)
        if hashlib.sha256(data).hexdigest() != asset['digest'][7:].lower():
            raise ValueError('Codex download failed SHA-256 verification. Nothing was installed.')
        _line(job, 'SHA-256 verified. Checking the archive.')
        binary = executable_bytes(data, asset['name'])
        if job and job.get('cancel_requested'):
            raise ValueError('Installation cancelled.')
        await asyncio.to_thread(_write_binary, binary)
    _line(job, 'Codex installed. Next, sign in with ChatGPT.')
    return {'ok': True}


def begin_install():
    if any(j['state'] == 'running' for j in _jobs.values()):
        raise ValueError('Codex installation is already in progress.')
    # Keep only recent jobs and a bounded tail of output.
    while len(_jobs) >= 10:
        _jobs.pop(next(iter(_jobs)))
    job_id = uuid.uuid4().hex
    job = {'state': 'running', 'lines': deque(maxlen=100), 'permission': None, 'error': None, 'cancel_requested': False}
    _jobs[job_id] = job
    job['task'] = asyncio.create_task(_run_install_job(job_id, job))
    return {'id': job_id}


async def _run_install_job(job_id, job):
    surface = 'model-setup:' + job_id
    queue = permissions.open_channel(surface)
    install = asyncio.create_task(install_codex(surface, job))
    prompt = None
    try:
        while not install.done():
            prompt = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait([install, prompt], return_when=asyncio.FIRST_COMPLETED)
            if prompt in done:
                job['permission'] = prompt.result()
            else:
                prompt.cancel()
                await asyncio.gather(prompt, return_exceptions=True)
        await install
        job['state'] = 'done'
    except Exception as error:
        job['state'] = 'error'
        # HTTP errors can contain remote text; do not relay response bodies.
        job['error'] = str(error) if isinstance(error, (ValueError, OSError)) else 'Codex installation failed. Please try again.'
    finally:
        if prompt and not prompt.done():
            prompt.cancel()
        permissions.close_channel(surface)
        job['permission'] = None
        if not install.done():
            install.cancel()
        await asyncio.gather(install, *( [prompt] if prompt else []), return_exceptions=True)


def install_progress(job_id):
    job = _jobs.get(job_id)
    if job is None:
        raise KeyError('Installation not found.')
    pending = job['permission']
    if pending and pending['id'] not in permissions._pending:
        pending = None
    return {'id': job_id, 'state': job['state'], 'lines': list(job['lines']),
            'permission': pending, 'error': job['error']}


def cancel_install(job_id):
    job = _jobs.get(job_id)
    if not job:
        raise KeyError('Installation not found.')
    # Only cancel while waiting for approval. npm cannot be cancelled by
    # cancelling a to_thread future while its process is still installing.
    pending = job['permission']
    job['cancel_requested'] = True
    if pending and pending['id'] in permissions._pending:
        permissions.answer(pending['id'], 'reject', 'model-setup')
    return {'ok': True}


def _mac_launcher(args):
    # Terminal opens executable files, but does not forward CLI arguments.
    # A short Python .command executable preserves `codex login` as argv.
    # Its contents are Python, never a shell command or AppleScript string.
    folder = managed_bin()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('sign-in-' + uuid.uuid4().hex + '.command')
    source = (f'#!/usr/bin/env -S {shlex.quote(sys.executable)}\n'
              'import pathlib, subprocess\n'
              'try:\n'
              f'    subprocess.run({args!r}, shell=False)\n'
              'finally:\n'
              '    pathlib.Path(__file__).unlink(missing_ok=True)\n')
    path.write_text(source, encoding='utf-8')
    path.chmod(0o700)
    return str(path)


def open_sign_in(kind):
    if kind == 'claude':
        path, _ = _claude_path()
        args = [path] if path else []
    elif kind == 'codex':
        path = find_codex()
        args = [path, 'login'] if path else []
    else:
        raise ValueError('Choose Claude or Codex.')
    if not args:
        raise ValueError('The sign-in app is not installed on this computer.')
    system = platform.system()
    kwargs = {'shell': False, 'env': child_env(0)}
    if system == 'Windows':
        kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
    elif system == 'Darwin':
        args = ['open', '-a', 'Terminal', _mac_launcher(args)]
    else:
        terminal = next((shutil.which(t) for t in ('x-terminal-emulator', 'gnome-terminal', 'xterm') if shutil.which(t)), None)
        if not terminal:
            raise ValueError('No terminal app was found. Install a terminal, then try sign-in again.')
        args = [terminal, *(['--'] if Path(terminal).name == 'gnome-terminal' else ['-e']), *args]
    subprocess.Popen(args, **kwargs)
    return {'ok': True, 'message': 'Finish signing in in the window that opened.'}


async def wait_signed_in(kind, timeout=120):
    if kind not in ('claude', 'codex'):
        raise ValueError('Choose Claude or Codex.')
    deadline = time.monotonic() + max(0, min(timeout, 300))
    while True:
        current = await asyncio.to_thread(status)
        if current[kind]['signed_in']:
            return {'signed_in': True, 'account': current[kind].get('account')}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'signed_in': False, 'timed_out': True}
        await asyncio.sleep(min(2, remaining))


def provider_preset(provider):
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise ValueError('Choose OpenAI, Anthropic, OpenRouter, or Google.')


async def test_api_key(provider, key):
    _, url, model = provider_preset(provider)
    if not key or not key.strip():
        return {'ok': False, 'error': 'Paste an API key first.'}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # One request through the same provider transports as Settings.
            await openai_compatible._post_chat(client, url, key, {
                'model': model, 'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'max_tokens': 16})
        return {'ok': True}
    except httpx.HTTPStatusError as error:
        return {'ok': False, 'error': f'The provider refused the test (HTTP {error.response.status_code}). Check your key and billing.'}
    except (httpx.HTTPError, ValueError, KeyError):
        return {'ok': False, 'error': 'The key could not be tested. Check your key and connection, then try again.'}


async def create_connection(kind, **fields):
    kind = {'claude_cli': 'claude', 'codex_cli': 'codex'}.get(kind, kind)
    if kind in ('claude', 'codex'):
        current = await asyncio.to_thread(status)
        if not current[kind]['signed_in'] or not current[kind].get('available', current[kind].get('installed')):
            raise ValueError('Finish signing in before connecting.')
        endpoint_kind = kind + '_cli'
        existing = current['connections'].get(endpoint_kind) or []
        endpoint = existing[0] if existing else model_endpoints.create_endpoint(
            name='Claude Code' if kind == 'claude' else 'ChatGPT with Codex', kind=endpoint_kind)
    elif kind == 'api':
        name, url, model = provider_preset(fields.get('provider'))
        key = fields.get('key', '')
        tested = await test_api_key(fields.get('provider'), key)
        if not tested['ok']:
            raise ValueError(tested['error'])
        endpoint = model_endpoints.create_endpoint(name=name, base_url=url, model=model, api_key=key, kind='api')
    else:
        raise ValueError('Choose Claude, Codex, or an API key. Local setup is in Cookbook.')
    # No default model (David, 2026-08-31): each chat picks its own.
    return {'ok': True, 'connection': endpoint}
