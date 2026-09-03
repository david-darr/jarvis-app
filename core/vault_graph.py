"""Vault graph — the Brain tab's Vault view (David's ask 2026-09-01: "kind of
similar to the VAULT tab in our original jarvis kiosk"). Ported from the real
kiosk implementation (voice-visualizer/server.py's build_vault_graph() /
_vault_real_path() / read_vault_note() / write_vault_note()), adapted to use
core/vault.py's resolve_vault_dir() instead of a hardcoded path, since
jarvis-app's vault location is user-configurable.

Two edge kinds: "contains" (folder -> direct child) draws the browsable tree
backbone; "link" (note -> note) comes from [[wikilink]] text matched against
note basenames case-insensitively, same resolution Obsidian itself uses.
"""
import os
import re
from typing import Optional

from core.vault import resolve_vault_dir

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_SKIP_DIRS = {".obsidian", ".trash", ".git"}


def _vault_real_path(rel_path: str) -> Optional[str]:
    """Resolves a vault-relative path and confirms the result is still
    inside the vault root — blocks ../ traversal and absolute-path tricks
    regardless of how os.path.join handles a leading slash, since the
    startswith check below is what actually decides, not the join."""
    if not isinstance(rel_path, str):
        return None
    vault_root = resolve_vault_dir()
    candidate = os.path.realpath(os.path.join(vault_root, rel_path))
    root = os.path.realpath(vault_root)
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def build_vault_graph() -> dict:
    """Walks the vault fresh on every request — a few hundred small .md
    files is cheap enough not to bother caching."""
    vault_root = resolve_vault_dir()
    nodes: list[dict] = []
    edges: list[dict] = []
    by_basename: dict[str, str] = {}

    for dirpath, dirnames, filenames in os.walk(vault_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, vault_root)
        dir_id = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        nodes.append({"id": dir_id, "type": "folder", "name": "Vault" if dir_id == "" else os.path.basename(dirpath)})
        if dir_id:
            parent = dir_id.rsplit("/", 1)[0] if "/" in dir_id else ""
            edges.append({"source": parent, "target": dir_id, "kind": "contains"})
        for fname in sorted(filenames):
            if not fname.lower().endswith(".md"):
                continue
            rel_path = fname if dir_id == "" else f"{dir_id}/{fname}"
            stem = fname[:-3]
            nodes.append({"id": rel_path, "type": "note", "name": stem, "folder": dir_id})
            edges.append({"source": dir_id, "target": rel_path, "kind": "contains"})
            by_basename.setdefault(stem.lower(), rel_path)

    seen_links = set()
    for node in nodes:
        if node["type"] != "note":
            continue
        try:
            with open(os.path.join(vault_root, node["id"]), "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        for m in _WIKILINK_RE.finditer(text):
            target_id = by_basename.get(m.group(1).strip().lower())
            if not target_id or target_id == node["id"]:
                continue
            key = (node["id"], target_id)
            if key in seen_links:
                continue
            seen_links.add(key)
            edges.append({"source": node["id"], "target": target_id, "kind": "link"})

    return {"nodes": nodes, "edges": edges}


def read_vault_note(rel_path: str) -> Optional[str]:
    real = _vault_real_path(rel_path)
    if not real or not real.lower().endswith(".md"):
        return None
    try:
        with open(real, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_vault_note(rel_path: str, content: str) -> bool:
    """Can only overwrite an EXISTING note, never create one — same
    deliberate restriction as the original kiosk implementation, so this
    endpoint can't be used to plant arbitrary new files in the vault."""
    real = _vault_real_path(rel_path)
    if not real or not real.lower().endswith(".md") or not os.path.isfile(real):
        return False
    with open(real, "w", encoding="utf-8") as f:
        f.write(content)
    return True
