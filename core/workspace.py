"""Workspace confinement — David's ask 2026-08-31, ported from Odysseus's
src/tool_execution.py + routes/workspace_routes.py (real repo, cross-checked
line for line, not guessed).

A session can be pinned to a folder ("workspace"). While pinned, that folder
becomes the agent's cwd for that session (see core/brain.py's cwd_override
and services/chat_service.py) instead of the vault dir, so its file/shell
tools operate there. vet_workspace() is what makes this safe to expose to a
picker: rejects non-directories, sensitive dirs (.ssh, .gnupg, shell rc/env
files), and filesystem roots (binding "/" or "C:\\" would collapse
confinement into host-wide access).
"""
import os

_SENSITIVE_BASENAMES: set[str] = {
    ".ssh", ".gnupg", ".gitconfig",
    ".bashrc", ".bash_profile", ".bash_logout",
    ".zshrc", ".zprofile", ".zshenv",
    ".profile", ".tcshrc", ".cshrc",
    ".env", ".netrc",
}
_SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    "authorized_keys", "id_rsa", "id_ed25519", "id_ecdsa", "known_hosts",
)
_SENSITIVE_BASENAMES_CF = frozenset(b.casefold() for b in _SENSITIVE_BASENAMES)
_SENSITIVE_FILE_PATTERNS_CF = frozenset(p.casefold() for p in _SENSITIVE_FILE_PATTERNS)

_MAX_BROWSE_DIRS = 500


def _is_sensitive_path(resolved: str) -> bool:
    parts = [p.casefold() for p in resolved.split(os.sep)]
    filename = parts[-1] if parts else ""
    for part in parts:
        if part in _SENSITIVE_BASENAMES_CF:
            return True
    return filename in _SENSITIVE_FILE_PATTERNS_CF


def vet_workspace(raw: str) -> str | None:
    """Validate a requested workspace path. Returns the canonical (realpath)
    directory, or None if it's unusable: not a real directory, a sensitive
    path, or a filesystem root."""
    raw = (raw or "").strip()
    if not raw:
        return None
    resolved = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(resolved) or _is_sensitive_path(resolved):
        return None
    # A filesystem root is its own dirname (covers "/", "C:\\", "\\\\server\\share").
    # Binding one as a workspace would make every absolute path "inside" it,
    # collapsing confinement into host-wide access.
    if os.path.dirname(resolved) == resolved:
        return None
    return resolved


def browse_dir(path: str) -> dict:
    """List subdirectories of `path` (default: home) for the workspace picker.
    Directories only, hidden entries and symlinked dirs skipped."""
    target = os.path.realpath(os.path.expanduser((path or "").strip() or "~"))
    if not os.path.isdir(target):
        target = os.path.realpath(os.path.expanduser("~"))

    dirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                except OSError:
                    continue
    except (PermissionError, OSError):
        dirs = []

    dirs_sorted = sorted(dirs, key=lambda d: d["name"].lower())
    truncated = len(dirs_sorted) > _MAX_BROWSE_DIRS
    parent = os.path.dirname(target)
    return {
        "path": target,
        "parent": parent if parent and parent != target else None,
        "dirs": dirs_sorted[:_MAX_BROWSE_DIRS],
        "truncated": truncated,
        "selectable": vet_workspace(target) is not None,
    }
