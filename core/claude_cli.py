"""Prefer a newer, separately installed Claude Code over the SDK's frozen copy.

Anthropic's native installer manages ~/.local/bin/claude and updates it in the
background. The Windows npm package also has a real claude.exe behind its
claude.cmd shim; the SDK cannot launch that shim directly. The SDK otherwise
chooses its bundled CLI first. Returning None preserves that fallback for
clean JARVIS installs and for older or broken separate installations.
"""

from functools import lru_cache
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

import claude_agent_sdk


def _version(path: Path) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\s|$)", result.stdout.strip())
    return tuple(map(int, match.groups())) if match else None


@lru_cache(maxsize=4)
def _bundled_version(path: Path) -> tuple[int, int, int] | None:
    # The SDK installation cannot change while this backend is running.
    return _version(path) if path.is_file() else None


def preferred_cli_path() -> str | None:
    """Return the newest separate executable, or let the SDK use its bundle."""
    name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    bundled_version = _bundled_version(bundled)

    native = Path.home() / ".local" / "bin" / name
    # The native installer uses this stable launcher path. Keep the launcher,
    # rather than its resolved versioned target, so its updates take effect.
    candidates = [native]
    if platform.system() == "Windows":
        # npm's claude.cmd cannot be passed to the SDK on Windows. Its sibling
        # package contains the actual executable; no shell or wrapper runs.
        shim = shutil.which("claude.cmd")
        if shim:
            candidates.append(Path(shim).parent / "node_modules" / "@anthropic-ai"
                              / "claude-code" / "bin" / "claude.exe")
    best_path = None
    best_version = bundled_version
    for candidate in candidates:
        if not candidate.is_file():
            continue
        version = _version(candidate)
        if version and (best_version is None or version > best_version):
            best_path, best_version = candidate, version
    return str(best_path) if best_path else None
