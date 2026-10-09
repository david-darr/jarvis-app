"""Forge chats, isolated worktrees and confined review operations."""
import difflib
import hashlib
import os
import re
import threading
import uuid
from pathlib import Path

from core import file_checkpoints, model_endpoints
from core.session_manager import session_manager
from core.untrusted import wrap_untrusted
from services import forge_git, forge_projects as projects_module

TEXT_CAP = 1024 * 1024
INSTRUCTIONS_CAP = 32 * 1024


class ReviewConflict(ValueError):
    """The review no longer matches the current worktree."""


def _linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def confined(root, name, directory=False):
    """Reject escapes, Git metadata, Windows aliases and every linked component."""
    if not isinstance(name, str) or '\\' in name or ':' in name or '\0' in name:
        raise ValueError('Use a relative worktree path with forward slashes.')
    parts = name.split('/') if name else []
    if any(p in ('', '.', '..') or p.casefold() == '.git' or p.endswith(('.', ' '))
           or re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', p, re.I)
           or any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError('Unsafe worktree path.')
    if not parts and not directory:
        raise ValueError('Choose a file in the worktree.')
    path = root
    for part in parts:
        path = path / part
        if _linked(path):
            raise ValueError('Links cannot be opened or reverted.')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Path escapes the worktree.')
    return path


def _hunks(path, patch):
    lines = patch.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith('@@ ')]
    result = []
    for n, start in enumerate(starts):
        body = ''.join(lines[start:starts[n + 1] if n + 1 < len(starts) else len(lines)])
        match = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', lines[start])
        if not match:
            raise ValueError('Could not parse Git hunk.')
        result.append(dict(hash=hashlib.sha256((path + '\0' + body).encode()).hexdigest(),
                           old_start=int(match[1]), old_count=int(match[2] or 1),
                           new_start=int(match[3]), new_count=int(match[4] or 1), patch=body))
    return ''.join(lines[:starts[0]]) if starts else patch, result


def _new_patch(name, text):
    def quote(value):
        import json
        return json.dumps(value, ensure_ascii=False)
    lines = list(difflib.unified_diff([], text.splitlines(keepends=True),
                                    fromfile='/dev/null', tofile=quote('b/' + name)))
    body = ''
    for line in lines:
        body += line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
    return f'diff --git {quote("a/" + name)} {quote("b/" + name)}\nnew file mode 100644\n' + body if lines else ''


class ForgeSessions:
    def __init__(self, projects=None, sessions=None):
        self.projects = projects or projects_module.forge_projects
        self.sessions = sessions or session_manager
        self.lock = threading.RLock()

    def project(self, project_id):
        project = self.projects.get(project_id)
        if not project:
            raise KeyError('Project not found.')
        path = projects_module.vet_project(project['path'])
        code, actual = forge_git.run_git(path, 'repo', check=False)
        if code or os.path.normcase(actual.strip()) != os.path.normcase(path):
            raise ValueError('Forge sessions require a Git repository root with an initial commit.')
        return project, Path(path)

    def get(self, session_id):
        session = self.sessions.get_session(session_id)
        if not session or not session.get('forge'):
            raise KeyError('Forge session not found.')
        return session

    def list(self, project_id):
        if not self.projects.get(project_id):
            raise KeyError('Project not found.')
        rows = []
        for header in self.sessions.list_sessions(include_agents=True):
            session = self.sessions.get_session(header['id']) or {}
            if (session.get('forge') or {}).get('project_id') == project_id:
                rows.append({**header, 'forge': session['forge'], 'workspace_dir': session['workspace_dir']})
        return rows

    def branches(self, project_id):
        _, path = self.project(project_id)
        checked = {}
        for record in forge_git.run_git(path, 'worktrees')[1].split('\0\0'):
            fields = dict(item.split(' ', 1) for item in record.split('\0') if ' ' in item)
            if fields.get('branch'):
                checked[fields['branch'].removeprefix('refs/heads/')] = fields.get('worktree')
        return [dict(name=name, worktree=checked.get(name))
                for name in forge_git.run_git(path, 'branches')[1].splitlines() if name]

    def _destination(self, root, repo, group, leaf):
        root = Path(root).resolve()
        if any(Path(part).name != part or part in ('', '.', '..') for part in (group, leaf)):
            raise ValueError('Worktree path escapes the Forge root.')
        destination = root / '.worktrees' / group / leaf
        if not destination.resolve().is_relative_to(root) or destination.resolve() == root:
            raise ValueError('Worktree path escapes the Forge root.')
        for item in (destination, *destination.parents):
            if _linked(item):
                raise ValueError('Worktree paths cannot contain links.')
            if item == root:
                break
        resolved = destination.resolve()
        if resolved == repo or resolved.is_relative_to(repo) or repo.is_relative_to(resolved):
            raise ValueError('Worktrees must be outside the project repository.')
        projects_module._vet_root(resolved, projects_module._existing_ancestor(resolved))
        return destination

    def create(self, project_id, task, model_endpoint_id=None, mode='build', isolation='new_worktree',
               branch=None, base_branch=None, owner_user=None, model_override=None):
        if mode not in ('build', 'plan') or isolation not in ('new_worktree', 'existing_branch', 'in_place'):
            raise ValueError('Choose a valid mode and isolation.')
        if not task.strip():
            raise ValueError('Describe the task.')
        endpoint = model_endpoints.get_endpoint(model_endpoint_id) if model_endpoint_id else None
        if model_endpoint_id and not endpoint:
            raise ValueError('Model endpoint not found.')
        # The exact model, with the same rules as a chat's model picker
        # (routes/session_routes.py set_session_model).
        if model_override is not None:
            model_override = model_override.strip() or None
        if model_override is not None:
            if not endpoint or endpoint['kind'] not in ('claude_cli', 'codex_cli', 'api'):
                raise ValueError('Exact models are available only for CLI and API agents.')
            if len(model_override) > 160 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]*', model_override):
                raise ValueError('Enter a valid model ID (160 characters maximum).')
        with self.lock:
            project, repo = self.project(project_id)
            current = forge_git.run_git(repo, 'branch', check=False)[1].strip() or 'HEAD'
            base_branch = base_branch or current
            if isolation == 'in_place':
                base_branch = current
            if isolation == 'existing_branch':
                if not branch or branch not in [b['name'] for b in self.branches(project_id)]:
                    raise ValueError('Choose an existing local branch.')
                forge_git.validate_branch(repo, branch)
            base_commit = forge_git.rev_parse(repo, branch if isolation == 'existing_branch' else base_branch)
            worktree, root = repo, None
            if isolation != 'in_place':
                root = projects_module.forge_root(create=True)
                short = uuid.uuid4().hex[:6]
                slug = projects_module.folder_name(task)[:48].lower()
                if not re.fullmatch(r'[0-9a-f]{12}', project['id']):
                    raise ValueError('Invalid project ID.')
                worktree = self._destination(root, repo, f"{repo.name}-{project['id']}", f'{slug}-{short}')
                worktree.parent.mkdir(parents=True, exist_ok=True)
                self._destination(root, repo, worktree.parent.name, worktree.name)
                if isolation == 'new_worktree':
                    branch = forge_git.validate_branch(repo, f'forge/{slug}-{short}')
                    forge_git.worktree_add(repo, worktree, base_commit, branch)
                else:
                    forge_git.worktree_add(repo, worktree, branch)
            else:
                branch = current
            try:
                projects_module.vet_project(str(worktree))
                session = self.sessions.create_session(task.strip()[:120], owner_user=owner_user)
                self.sessions.set_workspace(session['id'], str(worktree))
                self.sessions.set_model_endpoint(session['id'], model_endpoint_id, model_override)
                details = dict(project_id=project_id, worktree=str(worktree), branch=branch,
                               base_branch=base_branch, base_commit=base_commit, mode=mode, isolation=isolation)
                if root:
                    details['forge_root'] = str(root)
                self.sessions.set_forge(session['id'], details)
            except Exception:
                if worktree != repo:
                    forge_git.worktree_remove(repo, worktree)
                raise
            return self.get(session['id'])

    def workspace(self, session_id):
        session = self.get(session_id)
        details = session['forge']
        if details.get('removed'):
            raise ValueError('This Forge session has ended.')
        _, repo = self.project(details['project_id'])
        raw = Path(details['worktree'])
        if session.get('workspace_dir') != str(raw):
            raise ValueError('Forge workspace no longer matches its session.')
        if details['isolation'] == 'in_place':
            if raw != repo:
                raise ValueError('In-place workspace no longer matches the project.')
        else:
            root = Path(details['forge_root'])
            expected = self._destination(root, repo, f"{repo.name}-{details['project_id']}", raw.name)
            if raw != expected or not re.fullmatch(r'[a-z0-9_-]+-[0-9a-f]{6}', raw.name):
                raise ValueError('Worktree path escapes the Forge root.')
            registered = forge_git.run_git(repo, 'worktrees')[1]
            if f'worktree {raw}\0' not in registered and f'worktree {raw.as_posix()}\0' not in registered:
                raise ValueError('Worktree is no longer registered with this project.')
        root = Path(projects_module.vet_project(str(raw)))
        if root != raw or _linked(raw / '.git'):
            raise ValueError('Worktree root or Git metadata cannot be a link.')
        actual = forge_git.run_git(root, 'repo')[1].strip()
        if os.path.normcase(actual) != os.path.normcase(str(root)):
            raise ValueError('Git working directory no longer matches this session.')
        def common(folder):
            return (folder / forge_git.run_git(folder, 'common')[1].strip()).resolve()
        if common(root) != common(repo):
            raise ValueError('Git metadata no longer belongs to this project.')
        return root

    def set_mode(self, session_id, mode):
        if mode not in ('build', 'plan'):
            raise ValueError('Choose Build or Plan.')
        with self.lock:
            self.workspace(session_id)
            session = self.get(session_id)
            return self.sessions.set_forge(session_id, {**session['forge'], 'mode': mode})

    def changes(self, session_id):
        root = self.workspace(session_id)
        base = self.get(session_id)['forge']['base_commit']
        rows = []
        for record in forge_git.diff(root, base, numstat=True).split('\0'):
            if not record:
                continue
            added, removed, name = record.split('\t', 2)
            added = added.rsplit('\n', 1)[-1]  # Git may emit a line-ending warning first.
            try:
                confined(root, name)
            except ValueError as error:
                rows.append(dict(path=name, added=None, removed=None, binary=True,
                                 untracked=False, patch='', hunks=[], unavailable=str(error)))
                continue
            patch = forge_git.diff(root, base, name)
            _, hunks = _hunks(name, patch)
            rows.append(dict(path=name, added=int(added) if added.isdigit() else None,
                             removed=int(removed) if removed.isdigit() else None,
                             binary=added == '-', untracked=False, patch=patch, hunks=hunks))
        for name in forge_git.run_git(root, 'untracked')[1].split('\0'):
            if not name:
                continue
            try:
                text = self._text(confined(root, name))
                patch = _new_patch(name, text)
                _, hunks = _hunks(name, patch)
                rows.append(dict(path=name, added=len(text.splitlines()), removed=0, binary=False,
                                 untracked=True, patch=patch, hunks=hunks))
            except ValueError:
                rows.append(dict(path=name, added=None, removed=None, binary=True,
                                 untracked=True, patch='', hunks=[]))
        return dict(base_commit=base, files=sorted(rows, key=lambda r: r['path']))

    @staticmethod
    def _confirmed(confirmed):
        if confirmed is not True:
            raise ValueError('Explicit confirmed=true is required.')

    def revert_hunk(self, session_id, name, hunk_hash, confirmed=False):
        self._confirmed(confirmed)
        with self.lock:
            root = self.workspace(session_id)
            confined(root, name)
            row = next((r for r in self.changes(session_id)['files'] if r['path'] == name), None)
            hunk = next((h for h in (row or {}).get('hunks', []) if h['hash'] == hunk_hash), None)
            if not hunk:
                raise ReviewConflict('This hunk changed. Refresh Changes before reverting it.')
            header, _ = _hunks(name, row['patch'])
            header = ''.join(line for line in header.splitlines(keepends=True)
                             if not line.startswith(('old mode ', 'new mode ')))
            try:
                forge_git.apply_reverse(root, header + hunk['patch'])
            except forge_git.GitError as error:
                raise ReviewConflict('The hunk could not be reverted. Refresh Changes. ' + str(error)) from error
            return {'ok': True}

    def revert_file(self, session_id, name, confirmed=False):
        self._confirmed(confirmed)
        with self.lock:
            root = self.workspace(session_id)
            path = confined(root, name)
            untracked = forge_git.run_git(root, 'untracked')[1].split('\0')
            if name in untracked:
                if not path.is_file():
                    raise ValueError('Choose one regular untracked file.')
                path.unlink()
            else:
                base = self.get(session_id)['forge']['base_commit']
                forge_git.require_tracked(root, base, name)
                forge_git.restore_file(root, base, name)
            return {'ok': True}

    def checkpoints(self, session_id):
        workspace = self.get(session_id)['forge']['worktree']
        events = file_checkpoints.list_events(source=f'chat:{session_id}')
        for event in events:
            event['roots'] = [r for r in event['roots']
                              if os.path.normcase(r['path']) == os.path.normcase(workspace)]
        return events

    def undo(self, session_id, event_id, confirmed=False):
        self._confirmed(confirmed)
        with self.lock:
            root = self.workspace(session_id)
            event = file_checkpoints.get_event(event_id)
            if event['source'] != f'chat:{session_id}':
                raise KeyError('Checkpoint not found for this session.')
            selections = []
            for info in event['roots']:
                if os.path.normcase(info['path']) != os.path.normcase(str(root)):
                    continue
                for change in info.get('changes', []):
                    confined(root, change['path'])
                    selections.append({'root': info['path'], 'path': change['path']})
            if not selections:
                raise file_checkpoints.Conflict('This turn has no worktree changes to undo.')
            return {'restored': file_checkpoints.restore(event_id, selections)}

    def remove(self, session_id, discard=False, confirmed=False):
        from services.forge_apps import forge_apps
        from services.forge_terminals import forge_terminals
        if discard:
            self._confirmed(confirmed)
        with forge_apps.lock, forge_terminals.lock, self.lock:
            root = self.workspace(session_id)
            session = self.get(session_id)
            details = session['forge']
            if details['isolation'] != 'in_place':
                if not discard and forge_git.run_git(root, 'status')[1]:
                    raise ReviewConflict('Worktree has uncommitted changes. Confirm discarding them to remove it.')
                forge_apps.stop(session_id)
                forge_terminals.stop_session(session_id)
                _, repo = self.project(details['project_id'])
                forge_git.worktree_remove(repo, root, discard)
            else:
                forge_apps.stop(session_id)
                forge_terminals.stop_session(session_id)
            self.sessions.set_forge(session_id, {**details, 'removed': True})
            return {'ok': True, 'branch': details['branch'], 'kept_project_folder': details['isolation'] == 'in_place'}

    def files(self, session_id, name=''):
        root = self.workspace(session_id)
        folder = confined(root, name, directory=True)
        if not folder.is_dir():
            raise ValueError('Directory is unavailable.')
        changed = {r['path'] for r in self.changes(session_id)['files']}
        names = forge_git.run_git(root, 'files')[1].split('\0') + forge_git.run_git(root, 'untracked')[1].split('\0')
        prefix = name + '/' if name else ''
        entries = {}
        for filename in names:
            if not filename or not filename.startswith(prefix):
                continue
            child = prefix + filename[len(prefix):].split('/', 1)[0]
            try:
                path = confined(root, child)
                if not path.exists():
                    continue
            except ValueError:
                continue
            entries[child] = dict(name=path.name, path=child, directory=path.is_dir(),
                                 changed=child in changed or any(p.startswith(child + '/') for p in changed))
        return {'path': name, 'entries': sorted(entries.values(), key=lambda r: (not r['directory'], r['name'].casefold()))}

    @staticmethod
    def _text(path, cap=TEXT_CAP):
        if not path.is_file():
            raise ValueError('Choose a regular text file.')
        with path.open('rb') as handle:
            body = handle.read(cap + 1)
        if len(body) > cap:
            raise ValueError('File exceeds the text size limit.')
        if b'\0' in body:
            raise ValueError('Binary files cannot be opened as text.')
        try:
            return body.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('Binary or non-UTF-8 file cannot be opened as text.') from exc

    def file(self, session_id, name):
        root = self.workspace(session_id)
        return self._editor_file(confined(root, name), name)

    @staticmethod
    def _editor_file(path, name):
        if not path.is_file():
            raise ValueError('Choose a regular file.')
        with path.open('rb') as handle:
            body = handle.read(TEXT_CAP + 1)
            stamp = os.fstat(handle.fileno()).st_mtime_ns
        if len(body) > TEXT_CAP:
            raise ValueError('File exceeds the text size limit.')
        binary = b'\0' in body
        try:
            content = body.decode('utf-8') if not binary else ''
        except UnicodeDecodeError:
            binary, content = True, ''
        return dict(path=name, content=content, binary=binary,
                    hash=hashlib.sha256(body).hexdigest(), mtime=str(stamp))

    def save_file(self, session_id, name, content, expected_hash, expected_mtime, overwrite=False):
        if not isinstance(content, str) or '\0' in content:
            raise ValueError('Save a UTF-8 text file without binary bytes.')
        body = content.encode('utf-8')
        if len(body) > TEXT_CAP:
            raise ValueError('File exceeds the text size limit.')
        with self.lock:
            root = self.workspace(session_id)
            path = confined(root, name)
            current = self._editor_file(path, name)
            if current['binary']:
                raise ValueError('Binary files are read only.')
            if not overwrite and (current['hash'] != expected_hash or current['mtime'] != expected_mtime):
                raise ReviewConflict('This file changed on disk. Reload it or overwrite with your edits.')
            # Revalidate before opening, and compare the opened file before any writes.
            path = confined(root, name)
            with path.open('r+b') as handle:
                existing = handle.read(TEXT_CAP + 1)
                stamp = str(os.fstat(handle.fileno()).st_mtime_ns)
                if hashlib.sha256(existing).hexdigest() != current['hash'] or stamp != current['mtime']:
                    raise ReviewConflict('This file changed while saving. Try again.')
                handle.seek(0)
                handle.write(body)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
                stamp = str(os.fstat(handle.fileno()).st_mtime_ns)
            return dict(path=name, content=content, binary=False,
                        hash=hashlib.sha256(body).hexdigest(), mtime=stamp)

    def repo_context(self, session_id):
        """First-turn repository references, preserved in sent-text history."""
        root = self.workspace(session_id)
        if self.get(session_id)['forge'].get('repo_instructions_loaded'):
            return ''
        remaining, parts = INSTRUCTIONS_CAP, []
        for name in ('AGENTS.md', 'CLAUDE.md'):
            try:
                path = confined(root, name)
                if not path.is_file():
                    continue
                with path.open('rb') as handle:
                    body = handle.read(remaining)
                remaining -= len(body)
                if b'\0' in body:
                    continue
                parts.append(wrap_untrusted('repository content: ' + name, body.decode('utf-8', errors='replace')))
            except (OSError, ValueError):
                continue
            if remaining <= 0:
                break
        if parts:
            self.sessions.set_untrusted_context(session_id, 'Forge repository content')
        return '\n\n' + '\n\n'.join(parts) if parts else ''

    def mark_repo_context_loaded(self, session_id):
        details = self.get(session_id)['forge']
        if not details.get('repo_instructions_loaded'):
            self.sessions.set_forge(session_id, {**details, 'repo_instructions_loaded': True})


forge_sessions = ForgeSessions()
