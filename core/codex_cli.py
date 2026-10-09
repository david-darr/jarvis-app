"""Shared executable discovery, including Kairos's verified user install."""
import os
from pathlib import Path
import platform
import shutil

from core.constants import BASE_DIR


def managed_bin() -> Path:
    # Packaged Electron supplies its user data directory. Development must
    # also install outside the checkout, even with the dev DATA_DIR default.
    configured = os.environ.get('JARVIS_DATA_DIR')
    if configured and not Path(configured).resolve().is_relative_to(Path(BASE_DIR).resolve()):
        return Path(configured) / 'bin'
    system = platform.system()
    if system == 'Windows':
        root = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    elif system == 'Darwin':
        root = Path.home() / 'Library' / 'Application Support'
    else:
        root = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return root / 'Kairos' / 'data' / 'bin'


def find_codex() -> str | None:
    name = 'codex.exe' if platform.system() == 'Windows' else 'codex'
    managed = managed_bin() / name
    if managed.is_file():
        return str(managed)
    found = shutil.which('codex')
    if not found:
        return None
    path = Path(found)
    if path.suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
        return str(path)
    # npm's Windows shim needs a shell. Find its real native binary instead.
    package = path.parent / 'node_modules' / '@openai' / 'codex'
    arch = 'aarch64' if platform.machine().lower() in {'arm64', 'aarch64'} else 'x86_64'
    triple = arch + '-pc-windows-msvc'
    variant = 'codex-win32-' + ('arm64' if arch == 'aarch64' else 'x64')
    roots = [package / 'vendor',
             package / 'node_modules' / '@openai' / variant / 'vendor',
             package.parent / variant / 'vendor']
    # Current packages use bin/, older releases used codex/. The installed
    # package's bin/codex.js names these same OS/architecture triples.
    candidates = [root / triple / directory / 'codex.exe'
                  for root in roots for directory in ('bin', 'codex')]
    return next((str(p) for p in candidates if p.is_file()), None)
