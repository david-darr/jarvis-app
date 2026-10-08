"""Folder-tab metadata, source snapshots and isolated Python packages.

Discovery owns policy (settings, overrides and approval); this module owns
the mechanics. The namespace deliberately has no search path: putting the
tabs root on it would let an approved tab import an unapproved neighbour.
"""
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import shutil
import sys
from types import ModuleType

SLUG = re.compile(r"^[a-z][a-z0-9_]*$")
FILES_URL = "/tab-files"
_registered = {}
_allowed = None


def is_link(path: str) -> bool:
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _p: False)(path)


def code_files(folder: str, *, user: bool) -> list[tuple[str, str]]:
    """Fail closed on links, including links disguised as bytecode caches.

    User caches are removed just as in the legacy loader. Shipped caches
    are ignored instead: an installed app directory may be read-only, and
    our source-only importer never reads or writes bytecode there anyway.
    """
    if is_link(os.path.dirname(folder)) or is_link(folder):
        raise ValueError("tab source root cannot be a symlink or junction")
    if not os.path.isdir(folder):
        raise ValueError("tab folder is missing")
    files = []
    for root, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs:
            if is_link(os.path.join(root, name)):
                raise ValueError("tab source directories cannot contain symlinks or junctions")
        if "__pycache__" in dirs and user:
            shutil.rmtree(os.path.join(root, "__pycache__"))
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(names):
            path = os.path.join(root, name)
            if is_link(path) or not os.path.isfile(path):
                raise ValueError("tab source files cannot be symlinks or junctions")
            files.append((os.path.relpath(path, folder).replace(os.sep, "/"), path))
    return sorted(files)


def fingerprint_files(files: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """Length-delimited paths and bytes; metadata alone cannot approve code."""
    digest = hashlib.sha256()
    paths = []
    for relative, path in files:
        encoded = relative.encode("utf-8")
        size = os.path.getsize(path)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        read_size = 0
        with open(path, "rb") as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(1024 * 1024):
                read_size += len(chunk)
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        if (read_size != size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            raise OSError(f"custom-tab source changed while fingerprinting: {relative}")
        paths.append(relative)
    return digest.hexdigest(), paths


def fingerprint(folder: str, *, user: bool = True) -> tuple[str, list[str]]:
    return fingerprint_files(code_files(folder, user=user))


def metadata(folder: str) -> dict:
    with open(os.path.join(folder, "tab.json"), encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("tab.json must be an object")
    slug = value.get("slug")
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ValueError("slug must match ^[a-z][a-z0-9_]*$")
    if slug != os.path.basename(folder):
        raise ValueError("slug must match the tab folder name")
    for key in ("name", "version", "description"):
        if not isinstance(value.get(key), str) or (key != "description" and not value[key].strip()):
            raise ValueError(f"{key} must be a string" + ("" if key == "description" else " and not empty"))
    if type(value.get("api")) is not int or value["api"] < 1:
        raise ValueError("api must be a positive integer")
    if not isinstance(value.get("hooks"), list) or any(not isinstance(h, str) for h in value["hooks"]):
        raise ValueError("hooks must be a list of strings")
    for key in ("icon_svg", "blurb", "detail", "reads"):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"{key} must be a string")
    # Unknown keys are deliberately inert, so later APIs can extend the file.
    return {key: value[key] for key in ("slug", "name", "version", "description", "api", "hooks",
                                      "icon_svg", "blurb", "detail", "reads") if key in value}


def scan(root: str, *, user: bool, api_version: int) -> list[dict]:
    if not os.path.isdir(root):
        return []
    entries = []
    for slug in sorted(os.listdir(root)):
        if slug == "__pycache__" or (user and slug in ("routes", "services", "views")
                and not os.path.lexists(os.path.join(root, slug, "tab.json"))):
            continue
        folder = os.path.join(root, slug)
        if not os.path.isdir(folder) and not is_link(folder):
            continue
        entries.append(inspect_folder(folder, user=user, api_version=api_version))
    return entries


def inspect_folder(folder: str, *, user: bool, api_version: int) -> dict:
    slug = os.path.basename(folder)
    entry = dict(slug=slug, name=slug, description="", blurb="", detail="", version="",
                 kind="user" if user else "prebuilt", format="folder" if user else "prebuilt",
                 enabled=None, status=None, reason=None, fingerprint=None, files=[], _path=folder,
                 _metadata={})
    try:
        snapshot, files = fingerprint(folder, user=user)
        meta = metadata(folder)
        # The metadata and the approval hash must describe the same bytes.
        if fingerprint(folder, user=user)[0] != snapshot:
            raise ValueError("tab source changed while reading tab.json")
        entry.update({key: meta[key] for key in ("name", "description", "version", "blurb", "detail", "reads")
                      if key in meta})
        entry.update(_metadata=meta, _snapshot=snapshot, files=files,
                     fingerprint=snapshot if user else None)
        if "routes.py" not in files:
            raise ValueError("tab must contain routes.py")
        if meta["api"] > api_version:
            entry.update(status="needs_newer_kairos", reason=f"tab requires API {meta['api']}; Kairos supports {api_version}")
    except (OSError, ValueError, UnicodeError) as exc:
        entry.update(status="invalid", reason=str(exc))
    return entry


def unregister(slug: str) -> None:
    from core import tab_hooks
    tab_hooks.retire(slug)
    prefix = f"kairos_tabs.{slug}"
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]
    parent = sys.modules.get("kairos_tabs")
    if parent is not None and hasattr(parent, slug):
        delattr(parent, slug)
    _registered.pop(slug, None)


def unregister_prebuilt(slug: str) -> None:
    entry = _registered.get(slug)
    if entry and entry["kind"] == "prebuilt":
        unregister(slug)


def register_all(entries: list[dict], allowed) -> None:
    """Register every eligible package before executing any tab's routes.

    Removing cached modules matters as much as refusing a fresh import:
    importlib otherwise returns a tab loaded before its approval was revoked.
    """
    global _allowed
    _allowed = allowed
    eligible = {e["slug"]: e for e in entries if e["status"] == "on"}
    for slug, old in list(_registered.items()):
        new = eligible.get(slug)
        if not new or (new["_path"], new["_snapshot"]) != (old["_path"], old["_snapshot"]):
            unregister(slug)
    if "kairos_tabs" not in sys.modules:
        root = ModuleType("kairos_tabs")
        root.__path__ = []
        root.__package__ = "kairos_tabs"
        root.__spec__ = importlib.util.spec_from_loader("kairos_tabs", loader=None, is_package=True)
        sys.modules["kairos_tabs"] = root
    if _finder not in sys.meta_path:
        sys.meta_path.insert(0, _finder)
    for slug, entry in eligible.items():
        if slug in _registered:
            continue
        name = f"kairos_tabs.{slug}"
        package = ModuleType(name)
        package.__path__ = [entry["_path"]]
        package.__package__ = name
        package.__spec__ = importlib.util.spec_from_loader(name, loader=None, is_package=True)
        package.__spec__.submodule_search_locations = package.__path__
        sys.modules[name] = package
        setattr(sys.modules["kairos_tabs"], slug, package)
        _registered[slug] = entry


class _SourceLoader(importlib.abc.Loader):
    def __init__(self, entry, path):
        self.entry, self.path = entry, path

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        entry = self.entry
        if not _allowed(entry):
            raise ImportError("tab is no longer approved or enabled")
        before = fingerprint(entry["_path"], user=entry["kind"] == "user")[0]
        with open(self.path, "rb") as handle:
            source = handle.read()
        after = fingerprint(entry["_path"], user=entry["kind"] == "user")[0]
        if before != after or after != entry["_snapshot"] or not _allowed(entry):
            raise ImportError("tab source changed while importing")
        module.__file__ = self.path
        # Compile source directly, including for lazy imports after startup.
        # sys.dont_write_bytecode alone still permits reading planted .pyc files.
        exec(compile(source, self.path, "exec"), module.__dict__)


class _TabFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("kairos_tabs."):
            return None
        parts = fullname.split(".")
        entry = _registered.get(parts[1])
        if entry is None or not _allowed(entry):
            raise ModuleNotFoundError(f"tab package is not approved or enabled: {parts[1]}")
        # Never defer to PathFinder, which accepts bytecode and native modules.
        relative = parts[2:]
        if not relative or any(not p.isidentifier() for p in relative):
            raise ModuleNotFoundError(fullname)
        base = os.path.join(entry["_path"], *relative)
        package = os.path.isfile(os.path.join(base, "__init__.py"))
        source = os.path.join(base, "__init__.py") if package else base + ".py"
        if not os.path.isfile(source):
            raise ModuleNotFoundError(fullname)
        return importlib.util.spec_from_file_location(fullname, source,
                    loader=_SourceLoader(entry, source), submodule_search_locations=[base] if package else None)


_finder = _TabFinder()


def import_routes(entry: dict) -> ModuleType:
    return importlib.import_module(f"kairos_tabs.{entry['slug']}.routes")


def file_bytes(entry: dict, filename: str, allowed) -> bytes | None:
    # A deliberately narrow public surface; Python and tab.json are review
    # material, not static assets to expose to every signed-in user.
    if filename not in ("view.js", "view.css") or not allowed(entry):
        return None
    try:
        before = fingerprint(entry["_path"], user=entry["kind"] == "user")[0]
        path = os.path.join(entry["_path"], filename)
        if is_link(path):
            return None
        with open(path, "rb") as handle:
            content = handle.read()
        after = fingerprint(entry["_path"], user=entry["kind"] == "user")[0]
        if before == after == entry["_snapshot"] and allowed(entry):
            return content
    except (OSError, ValueError):
        pass
    return None
