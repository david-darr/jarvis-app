"""Local Forge projects. Removing a record never removes its folder."""
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from core.atomic_io import read_json, write_json_atomic
from core.constants import BASE_DIR, DATA_DIR
from core.settings import get_setting, update_settings
from core.workspace import vet_workspace, _is_sensitive_path
from services import forge_git


def documents_folder():
    if os.name == 'nt':
        # The known-folder API follows redirected and OneDrive Documents.
        import ctypes
        from ctypes import wintypes
        class GUID(ctypes.Structure):
            _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                        ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]
        guid = GUID.from_buffer_copy(uuid.UUID('fdd39ad0-238f-46af-adb4-6c85480369c7').bytes_le)
        value = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(value)) == 0:
            try:
                return Path(value.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(value)
    home = Path.home()
    config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'user-dirs.dirs'
    if os.name != 'nt' and config.is_file():
        text = config.read_text(encoding='utf-8')
        match = re.search(r'^XDG_DOCUMENTS_DIR="([^"]+)"', text, re.M)
        if match:
            return Path(match[1].replace('$HOME', str(home))).expanduser()
    return home / 'Documents'


def vet_project(raw):
    path = vet_workspace(raw)
    if not path:
        raise ValueError('Choose a usable workspace folder.')
    candidate = Path(path)
    # App code and runtime credentials must not become an agent workspace.
    for protected in (Path(BASE_DIR).resolve(), Path(DATA_DIR).resolve()):
        if candidate == protected or candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
            raise ValueError("Kairos's own folders cannot be Forge projects.")
    return path


def forge_root(create=False):
    root = Path(get_setting('forge_root') or documents_folder() / 'Kairos Projects').expanduser().resolve()
    # Vet the nearest existing ancestor before creating anything.
    ancestor = _existing_ancestor(root)
    _vet_root(root, ancestor)
    if create:
        root.mkdir(parents=True, exist_ok=True)
        vet_project(str(root))
    return root


def set_root(raw):
    root = Path(raw.strip()).expanduser()
    if not raw.strip() or not root.is_absolute():
        raise ValueError('Enter an absolute folder path.')
    resolved = root.resolve()
    ancestor = _existing_ancestor(resolved)
    _vet_root(resolved, ancestor)
    update_settings(forge_root=str(resolved))
    return str(resolved)


def _existing_ancestor(path):
    while not path.exists():
        if path.parent == path:
            raise ValueError('Choose a Forge root on an available drive.')
        path = path.parent
    return path


def _vet_root(root, ancestor):
    if _is_sensitive_path(str(root)) or not vet_workspace(str(ancestor)):
        raise ValueError('Choose a usable Forge root folder.')
    for protected in (Path(BASE_DIR).resolve(), Path(DATA_DIR).resolve()):
        if root == protected or root.is_relative_to(protected) or protected.is_relative_to(root):
            raise ValueError("Kairos's own folders cannot be the Forge root.")


def folder_name(name):
    safe = re.sub(r'[^a-zA-Z0-9_-]+', '-', name.strip()).strip('-_')[:80]
    if not safe or safe.lower() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}:
        raise ValueError('Choose a project name with letters or numbers.')
    return safe


def validate_clone_url(raw):
    value = raw.strip()
    if not value or value.startswith('-') or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('Use an HTTPS or git@host:owner/repo URL.')
    if value.startswith('https://'):
        url = urlsplit(value)
        if url.hostname and not url.username and not url.password and url.path not in ('', '/') and not url.query and not url.fragment:
            return value
    if re.fullmatch(r'git@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+', value):
        if all(p not in ('.', '..', '') and not p.startswith('-') for p in value.split(':', 1)[1].split('/')):
            return value
    raise ValueError('Use an HTTPS or git@host:owner/repo URL.')


class ForgeProjects:
    def __init__(self, file=None):
        self.file = str(file or Path(DATA_DIR) / 'forge' / 'projects.json')
        self.lock = threading.RLock()

    def list(self):
        with self.lock:
            return sorted(read_json(self.file, []), key=lambda p: -(p.get('last_opened_at') or p['added_at']))

    def get(self, project_id):
        return next((p for p in self.list() if p['id'] == project_id), None)

    def add(self, path, name=''):
        path = vet_project(path)
        with self.lock:
            rows = self.list()
            existing = next((p for p in rows if os.path.normcase(p['path']) == os.path.normcase(path)), None)
            if existing:
                return existing
            item = dict(id=uuid.uuid4().hex[:12], name=name.strip()[:120] or Path(path).name,
                        path=path, added_at=time.time(), last_opened_at=None)
            write_json_atomic(self.file, [*rows, item])
            return item

    def remove(self, project_id):
        with self.lock:
            write_json_atomic(self.file, [p for p in self.list() if p['id'] != project_id])

    def opened(self, project_id):
        with self.lock:
            rows = self.list()
            item = next((p for p in rows if p['id'] == project_id), None)
            if not item:
                raise KeyError(project_id)
            vet_project(item['path'])
            item['last_opened_at'] = time.time()
            write_json_atomic(self.file, rows)
            return item

    def set_app_command(self, project_id, command):
        with self.lock:
            rows = self.list()
            item = next((p for p in rows if p['id'] == project_id), None)
            if not item:
                raise KeyError('Project not found.')
            if item.get('app_command') != command:
                item.pop('app_command_hash', None)
            item['app_command'] = command
            write_json_atomic(self.file, rows)
            return item

    def approve_app_command(self, project_id, command, digest):
        with self.lock:
            rows = self.list()
            item = next((p for p in rows if p['id'] == project_id), None)
            if not item or item.get('app_command') != command:
                raise ValueError('The app command changed during approval.')
            item['app_command_hash'] = digest
            write_json_atomic(self.file, rows)

    def new(self, name):
        destination = forge_root(create=True) / folder_name(name)
        destination.mkdir()  # exclusive: existing folders are never reused
        try:
            forge_git.init_repo(destination)
            (destination / 'README.md').write_text('# ' + name.strip().replace('\n', ' ') + '\n', encoding='utf-8')
            (destination / '.gitignore').write_text('.env\n.env.*\n!.env.example\n.venv/\n__pycache__/\n*.py[cod]\nnode_modules/\ndist/\nbuild/\n.DS_Store\nThumbs.db\n', encoding='utf-8')
            forge_git.initial_commit(destination, ['README.md', '.gitignore'])
        except (OSError, forge_git.GitError):
            # Only the folder created just above; a retry can then reuse the name.
            forge_git.remove_reserved(destination)
            raise
        return self.add(str(destination), name)

    def clone(self, url, name=''):
        source = validate_clone_url(url)
        name = name.strip() or source.rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
        root = forge_root(create=True)
        destination = root / folder_name(name)
        forge_git.clone_repo(root, source, destination)
        return self.add(str(destination), name)

    def recent_workspaces(self):
        from core.session_manager import session_manager
        seen, rows = set(), []
        sessions = sorted(session_manager.list_sessions(include_agents=True),
                          key=lambda s: s.get('updated_at') or s.get('created_at') or 0, reverse=True)
        for session in sessions:
            full = session_manager.get_session(session['id']) or {}
            path = full.get('workspace_dir')
            if not path:
                continue
            try:
                path = vet_project(path)
            except ValueError:
                continue
            key = os.path.normcase(path)
            if key not in seen:
                seen.add(key)
                rows.append(dict(path=path, name=Path(path).name))
            if len(rows) >= 20:
                break
        return rows


forge_projects = ForgeProjects()
