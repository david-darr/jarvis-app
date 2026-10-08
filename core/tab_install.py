"""Portable folder tabs: bounded staging, static scans and unapproved installs.

Archives and public GitHub folders share one validation path. Staging lives
inside DATA_DIR; no imported code is executed. Confirmation is bound to the
reviewed content fingerprint, and never substitutes for source approval.
"""
import base64
import io
import json
import os
import shutil
import stat
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

import httpx

from core import custom_tabs, settings, tab_checks, tab_folders
from core.constants import DATA_DIR
from services import skills_guard

MAX_COMPRESSED = 5 * 1024 * 1024
MAX_UNPACKED = 20 * 1024 * 1024
MAX_FILES = 500
_LOCK = threading.RLock()
_COMPILED = {".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".class", ".wasm", ".bin"}


class ReviewRequired(ValueError):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail["report"])


@contextmanager
def _staging():
    if tab_folders.is_link(DATA_DIR):
        raise ValueError("Tab staging root cannot be a link")
    os.makedirs(DATA_DIR, exist_ok=True)
    path = Path(DATA_DIR) / ("tab-install-" + uuid.uuid4().hex)
    # Inherit DATA_DIR's ACL. Python 3.13's Windows private-temp ACL can
    # deny the very restricted token that created it access to its own files.
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


def _path(name):
    # Reject Windows aliases and ADS too: extraction must mean the same thing
    # on Windows and Unix, and each source path must name exactly one file.
    if not isinstance(name, str) or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Invalid archive path")
    parts = name.rstrip("/").split("/")
    if len(parts) < 1 or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in parts):
        raise ValueError("Absolute or traversing archive path")
    if not tab_folders.SLUG.fullmatch(parts[0]) or parts[0] in ("routes", "services", "views"):
        raise ValueError("Archive must contain one top-level tab slug folder")
    import re
    if any(re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.I) for p in parts):
        raise ValueError("Reserved filename")
    return PurePosixPath(*parts)


def unpack(content, stage):
    if len(content) > MAX_COMPRESSED:
        raise ValueError("Tab archive exceeds 5 MB compressed")
    total = count = 0
    slug = None
    seen = set()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if len(archive.infolist()) > MAX_FILES + 500:
            raise ValueError("Too many archive entries")
        for item in archive.infolist():
            relative = _path(item.orig_filename)
            if "__pycache__" in relative.parts:
                raise ValueError("Compiled caches are forbidden")
            if slug is not None and slug != relative.parts[0]:
                raise ValueError("Archive must contain exactly one top-level folder")
            slug = relative.parts[0]
            key = str(relative).casefold()
            if key in seen:
                raise ValueError("Duplicate archive path")
            seen.add(key)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG):
                raise ValueError("Links and special files are forbidden")
            if item.flag_bits & 1:
                raise ValueError("Encrypted archives are not supported")
            target = stage.joinpath(*relative.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if len(relative.parts) < 2:
                raise ValueError("Files must be inside the tab folder")
            count += 1
            total += item.file_size
            if count > MAX_FILES or total > MAX_UNPACKED:
                raise ValueError("Tab exceeds 500 files or 20 MB unpacked")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as output:
                remaining = item.file_size
                while chunk := source.read(min(65536, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("Archive size mismatch")
                    output.write(chunk)
                if remaining:
                    raise ValueError("Archive size mismatch")
    if not slug:
        raise ValueError("Empty tab archive")
    return stage / slug


def _github_json(url):
    # Never follow redirects or download_url supplied by repository content.
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, headers={"Accept": "application/vnd.github+json"}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_UNPACKED * 2:
                    raise ValueError("GitHub response exceeds limit")
    return json.loads(data)


def github_archive(url):
    parsed = urlsplit(url)
    parts = unquote(parsed.path).strip("/").split("/")
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
            or not 5 <= len(parts) <= 64 or parts[2] != "tree" or any(p in ("", ".", "..") for p in parts)):
        raise ValueError("Use https://github.com/<owner>/<repo>/tree/<ref>/<path>")
    owner, repo, _, ref, *folder_parts = parts
    slug = folder_parts[-1]
    _path(slug)
    folder_path = "/".join(folder_parts)
    buffer = io.BytesIO()
    count = total = entries = 0
    def fetch(path):
        return _github_json(f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
    # GitHub tree links do not delimit branch names containing '/'. Try
    # longer refs only after a 404, leaving at least the selected tab folder.
    while True:
        try:
            root_files = fetch(folder_path)
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404 or len(folder_parts) < 2:
                raise
            ref += "/" + folder_parts.pop(0)
            folder_path = "/".join(folder_parts)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        def walk(path, depth=0):
            nonlocal count, total, entries
            if depth > 50:
                raise ValueError("GitHub folder nesting exceeds limit")
            children = root_files if depth == 0 else fetch(path)
            if not isinstance(children, list):
                raise ValueError("GitHub link must identify a folder")
            for child in children:
                entries += 1
                if entries > 1000 or not isinstance(child, dict):
                    raise ValueError("Too many GitHub entries")
                name = child.get("name", "")
                if not isinstance(name, str) or "/" in name or len(_path(slug + "/" + name).parts) != 2:
                    raise ValueError("Invalid GitHub filename")
                child_path = path + "/" + name
                if child.get("path") != child_path:
                    raise ValueError("GitHub path escaped selected folder")
                if child.get("type") == "dir":
                    walk(child_path, depth + 1)
                elif child.get("type") == "file":
                    count += 1
                    if count > MAX_FILES or child.get("size", 0) > MAX_UNPACKED:
                        raise ValueError("GitHub tab exceeds limits")
                    file = fetch(child_path)
                    if not isinstance(file, dict) or file.get("type") != "file" or file.get("encoding") != "base64" or file.get("submodule_git_url"):
                        raise ValueError("Links and special GitHub files are forbidden")
                    content = base64.b64decode("".join(file.get("content", "").split()), validate=True)
                    if len(content) != file.get("size"):
                        raise ValueError("GitHub file size mismatch")
                    total += len(content)
                    if total > MAX_UNPACKED:
                        raise ValueError("GitHub tab exceeds 20 MB")
                    relative = child_path[len(folder_path) + 1:]
                    archive.writestr(slug + "/" + relative, content)
                    if buffer.tell() > MAX_COMPRESSED:
                        raise ValueError("GitHub tab exceeds 5 MB compressed")
                else:
                    raise ValueError("Links and special GitHub files are forbidden")
        walk(folder_path)
    return buffer.getvalue()


def scan(folder):
    findings = []
    for relative, filename in tab_folders.code_files(str(folder), user=False):
        path = Path(filename)
        content = path.read_bytes()
        if path.suffix.lower() in _COMPILED or b"\x00" in content:
            raise ValueError(f"Binaries and compiled files are forbidden: {relative}")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValueError(f"Non-text file: {relative}") from exc
        if any(ord(c) < 32 and c not in "\r\n\t\f" for c in text):
            raise ValueError(f"Binary file: {relative}")
        if path.suffix.lower() == ".py":
            tab_checks.check_python(text, relative)
        # No .skillignore loophole: scan every imported text file.
        findings.extend(skills_guard.scan_file(path, relative, force_text=True))
    verdict = skills_guard._determine_verdict(findings)
    return skills_guard.ScanResult(folder.name, "community", "community", verdict, findings,
        datetime.now(timezone.utc).isoformat(), f"Tab scan: {verdict}")


def install(content, *, replace=False, confirmed=False, expected_fingerprint=None):
    with _LOCK, _staging() as temporary:
        folder = unpack(content, temporary)
        meta = tab_folders.metadata(str(folder))
        if not (folder / "routes.py").is_file():
            raise ValueError("Tab must contain routes.py")
        slug = meta["slug"]
        if slug in custom_tabs.template_slugs():
            raise ValueError("Cannot install over a prebuilt tab")
        target = Path(custom_tabs.USER_TABS_DIR) / slug
        legacy_exists = slug in custom_tabs._user_tab_slugs()
        if (target.exists() or legacy_exists) and not replace:
            raise ValueError("Tab already exists; choose replace explicitly")
        result = scan(folder)
        fingerprint, _ = tab_folders.fingerprint(str(folder))
        allowed, _ = skills_guard.should_allow_install(result, force=confirmed and expected_fingerprint == fingerprint)
        if not allowed:
            raise ReviewRequired({"needs_confirmation": result.verdict != "dangerous", "fingerprint": fingerprint,
                "report": skills_guard.format_scan_report(result), "findings": [vars(f) for f in result.findings]})
        if confirmed and expected_fingerprint != fingerprint:
            raise ValueError("Tab changed since scan; review it again")
        if tab_folders.is_link(str(target.parent)) or tab_folders.is_link(str(target)):
            raise ValueError("Tab destination cannot be a link")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Validate any existing tree before removal. Replacing removes all old
        # source and approval, while the tab's separate data store survives.
        if target.exists() or legacy_exists:
            custom_tabs.delete(slug)
        approvals = dict(settings.get_setting(custom_tabs._FOLDER_APPROVALS_KEY) or {})
        approvals.pop(slug, None)
        settings.update_settings(**{custom_tabs._FOLDER_APPROVALS_KEY: approvals})
        tab_folders.unregister(slug)
        os.rename(folder, target)
        return {"slug": slug, "status": "needs_approval", "verdict": result.verdict,
                "findings": [vars(f) for f in result.findings]}


def export(slug):
    if not tab_folders.SLUG.fullmatch(slug) or slug in custom_tabs.template_slugs():
        raise ValueError("Only folder user tabs can be exported")
    folder = Path(custom_tabs.USER_TABS_DIR) / slug
    files = tab_folders.code_files(str(folder), user=True)
    tab_folders.metadata(str(folder))
    before = tab_folders.fingerprint_files(files)[0]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, filename in files:
            archive.write(filename, slug + "/" + relative)
    if tab_folders.fingerprint(str(folder))[0] != before:
        raise ValueError("Tab changed during export; try again")
    return buffer.getvalue()
