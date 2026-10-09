"""Bounded, non-interactive git recipes for Forge. Never a shell."""
import copy
import os
import subprocess
import threading
from collections import Counter, defaultdict, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from core.workspace import vet_workspace

TIMEOUT = 20
OUTPUT_CAP = 16 * 1024 * 1024
_cache = OrderedDict()
_cache_lock = threading.RLock()


class GitError(ValueError):
    pass


def git_env():
    from services.forge_apps import child_env
    env = child_env()
    # Git's own identity and SSH agent are the person's credentials, not Kairos secrets.
    for key in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_AUTHOR_DATE',
                'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL', 'GIT_COMMITTER_DATE', 'SSH_AUTH_SOCK'):
        if key in os.environ: env[key] = os.environ[key]
    env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS=os.devnull,
               SSH_ASKPASS=os.devnull, GCM_INTERACTIVE='Never',
               GIT_SSH_COMMAND='ssh -oBatchMode=yes -oStrictHostKeyChecking=yes',
               GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    # The null device cannot execute: askpass always fails without a UI.
    # GIT_TERMINAL_PROMPT=0 also disables its terminal fallback.
    return env


def _run(cwd, args, timeout=TIMEOUT, output_cap=OUTPUT_CAP, check=True, input_data=None, worktree=False, hooks=False):
    folder = vet_workspace(str(cwd))
    if not folder:
        raise GitError('Project folder is unavailable or unusable.')
    command = ['git', '--no-pager', '--literal-pathspecs',
               *([] if hooks else ['-c', 'core.hooksPath=' + os.devnull]),
               '-c', 'credential.interactive=false', '-c', 'core.askPass=' + os.devnull,
               '-c', 'core.fsmonitor=false',
               '-c', 'protocol.ext.allow=never', *(['--work-tree=' + folder] if worktree else []), *args]
    output = bytearray()
    overflow = threading.Event()
    try:
        with subprocess.Popen(command, cwd=folder, env=git_env(), stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
            def consume():
                while chunk := process.stdout.read(65536):
                    room = max(0, output_cap - len(output))
                    output.extend(chunk[:room])
                    if len(chunk) > room:
                        overflow.set()
                        try:
                            process.kill()
                        except OSError:
                            pass  # Git may already have exited after its last output.
                        break
            reader = threading.Thread(target=consume, daemon=True)
            reader.start()
            def send():
                try:
                    remaining = memoryview(input_data)
                    while remaining:
                        written = process.stdin.write(remaining)
                        if not written:
                            break
                        remaining = remaining[written:]
                    process.stdin.close()
                except (OSError, BrokenPipeError):
                    pass
            writer = threading.Thread(target=send, daemon=True) if input_data is not None else None
            if writer:
                writer.start()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                reader.join(timeout=1)
                raise GitError('Git timed out. Try again with a smaller repository.')
            reader.join(timeout=1)
            if writer:
                writer.join(timeout=1)
            if reader.is_alive():
                raise GitError('Git output did not finish in time.')
            if overflow.is_set():
                raise GitError('Git output exceeded the dashboard limit.')
            value = output.decode('utf-8', errors='replace')
            if check and process.returncode:
                raise GitError(value.strip()[:1000] or 'Git failed.')
            return process.returncode, value
    except OSError as error:
        raise GitError(f'Git could not start: {error}') from error


# Exact read recipes: no arbitrary options such as --output or external diff.
READS = {
    'repo': ['rev-parse', '--show-toplevel'],
    'head': ['rev-parse', '--verify', 'HEAD'],
    'branch': ['symbolic-ref', '--short', '-q', 'HEAD'],
    'index': ['rev-parse', '--git-path', 'index'],
    'common': ['rev-parse', '--git-common-dir'],
    'files': ['ls-files', '-z', '--'],
    'untracked': ['ls-files', '--others', '--exclude-standard', '-z', '--'],
    'worktrees': ['worktree', 'list', '--porcelain', '-z'],
    'branches': ['for-each-ref', '--format=%(refname:short)', 'refs/heads/'],
    'status': ['status', '--porcelain=v1', '-z', '--untracked-files=all', '--'],
    'log': ['log', '--no-ext-diff', '--no-textconv', '--no-renames', '--format=%x00COMMIT%x00%H%x00%at%x00%aN%x00%aE%x00%s%x00', '--numstat', '-z', 'HEAD', '--'],
}


def run_git(path, recipe, **options):
    if recipe not in READS:
        raise GitError('Forge Preview supports read-only git summaries.')
    return _run(path, READS[recipe], **options)


def init_repo(path):
    return _run(path, ['init', '--', str(Path(path).resolve())])


def has_commits(path, *, any_branch=False):
    if any_branch:
        return bool(_run(path, ['rev-list', '--all', '--max-count=1'])[1].strip())
    return run_git(path, 'head', check=False)[0] == 0


def initial_commit(path, files=None):
    """Internal first commit, with a one-command identity fallback and no hooks."""
    if has_commits(path, any_branch=True):
        raise GitError('This project already has commits.')
    if not status_files(run_git(path, 'status')[1]):
        raise GitError('There are no files to commit.')
    identity = []
    for key in ('user.name', 'user.email'):
        code, value = _run(path, ['config', '--get', key], check=False)
        if code or not value.strip():
            identity = ['-c', 'user.name=Kairos', '-c', 'user.email=kairos@localhost']
    _run(path, ['add', '-A', '--', *(files if files is not None else ['.'])])
    return _run(path, [*identity, '-c', 'commit.gpgsign=false', 'commit', '-m', 'Initial commit'], timeout=120)


def validate_branch(path, branch):
    if not isinstance(branch, str) or not branch or branch.startswith('-') or any(ord(c) < 32 for c in branch):
        raise GitError('Invalid branch name.')
    _run(path, ['check-ref-format', '--branch', branch])
    return branch


def rev_parse(path, ref='HEAD'):
    if not isinstance(ref, str) or not ref or ref.startswith('-') or any(ord(c) < 32 for c in ref):
        raise GitError('Invalid base reference.')
    return _run(path, ['rev-parse', '--verify', '--end-of-options', ref + '^{commit}'])[1].strip()


def worktree_add(path, destination, base, branch=None):
    # Destination confinement belongs to ForgeSessions; this runner never accepts options.
    commit = rev_parse(path, base) if branch else validate_branch(path, base)
    args = ['worktree', 'add']
    if branch:
        args += ['-b', validate_branch(path, branch)]
    return _run(path, [*args, '--', str(destination), commit], timeout=120)


def worktree_remove(path, destination, discard=False):
    return _run(path, ['worktree', 'remove', *(['--force'] if discard else []), '--', str(destination)], timeout=120)


def diff(path, commit, filename=None, numstat=False):
    commit = rev_parse(path, commit)
    return _run(path, ['diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                      *(['--numstat', '-z'] if numstat else ['--patch', '--unified=3']),
                      commit, '--', *([filename] if filename else [])], worktree=True)[1]


def apply_reverse(path, patch):
    body = patch.encode('utf-8')
    _run(path, ['apply', '-R', '--check', '--', '-'], input_data=body, worktree=True)
    return _run(path, ['apply', '-R', '--', '-'], input_data=body, worktree=True)


def restore_file(path, commit, filename):
    # --no-overlay also removes tracked additions absent from the starting tree.
    return _run(path, ['checkout', '--no-overlay', rev_parse(path, commit), '--', filename], worktree=True)


def require_tracked(path, commit, filename):
    result = _run(path, ['ls-files', '--error-unmatch', '--with-tree=' + rev_parse(path, commit), '-z', '--', filename])
    if filename not in result[1].split('\0'):
        raise GitError('Choose one tracked file, not a directory.')
    return result


def clone_repo(root, source, destination):
    """Internal runner; the user-facing service validates URLs first."""
    root = Path(vet_workspace(str(root)) or '')
    destination = Path(destination).resolve()
    if not root.is_absolute() or destination.parent != root or destination.exists():
        raise GitError('Clone destination must be a new folder under the Forge root.')
    destination.mkdir()  # Reserve exclusively, including simultaneous clone requests.
    try:
        return _run(root, ['clone', '--', source, str(destination)], timeout=120)
    except GitError:
        # A failed clone must not leave a folder that blocks every retry.
        remove_reserved(destination)
        raise


def remove_reserved(folder):
    """Delete a folder this process just created, never following links."""
    import shutil
    folder = Path(folder)
    if folder.is_dir() and not folder.is_symlink():
        shutil.rmtree(folder, ignore_errors=True)


def status_files(text):
    records = iter(text.split('\0'))
    rows = []
    for record in records:
        if not record:
            continue
        if len(record) < 4 or record[2] != ' ':
            raise GitError('Git status could not be read.')
        index, working, name = record[0], record[1], record[3:]
        original = next(records, '') if index in 'RC' or working in 'RC' else None
        rows.append(dict(path=name, original=original, staged=index not in ' ?',
                         unstaged=working != ' ', index=index, working=working))
    return rows


def session_status(path):
    branch = run_git(path, 'branch', check=False)[1].strip()
    code, upstream = _run(path, ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'], check=False)
    ahead = behind = 0
    if not code:
        counts = _run(path, ['rev-list', '--left-right', '--count', 'HEAD...@{upstream}'])[1].split()
        ahead, behind = map(int, counts)
    return dict(branch=branch or 'Detached HEAD', upstream=upstream.strip() if not code else None,
                ahead=ahead, behind=behind, files=status_files(run_git(path, 'status')[1]),
                branches=run_git(path, 'branches')[1].splitlines())


def stage(path, filename=None, unstage=False):
    args = ['restore', '--staged'] if unstage else ['add', '-A']
    return _run(path, [*args, '--', filename if filename is not None else '.'])


def commit(path, message):
    if not isinstance(message, str) or not message.strip() or len(message) > 4000 or '\0' in message:
        raise GitError('Enter a commit message, up to 4,000 characters.')
    rows = session_status(path)['files']
    if not rows:
        raise GitError('There are no changes to commit.')
    if not any(row['staged'] for row in rows):
        stage(path)
    return _run(path, ['commit', '-m', message.strip()], timeout=120, hooks=True)


def switch_branch(path, branch, create=False):
    validate_branch(path, branch)
    if run_git(path, 'status')[1]:
        raise GitError('Commit or discard your worktree changes before switching branches.')
    return _run(path, ['switch', *(['-c'] if create else []), branch], hooks=True)


def push_target(path):
    branch = run_git(path, 'branch', check=False)[1].strip()
    if not branch:
        raise GitError('Choose a branch before pushing.')
    validate_branch(path, branch)
    code, configured = _run(path, ['config', '--get', f'branch.{branch}.remote'], check=False)
    remotes = _run(path, ['remote'])[1].splitlines()
    if not remotes:
        raise GitError('This project has no Git remote. Add one in Git before pushing.')
    remote = configured.strip() if not code else ('origin' if 'origin' in remotes else remotes[0] if len(remotes) == 1 else '')
    if not remote or remote == '.' or remote not in remotes or remote.startswith('-'):
        raise GitError('Choose a single Git remote for this branch before pushing.')
    urls = _run(path, ['remote', 'get-url', '--push', '--all', remote])[1].splitlines()
    if len(urls) != 1:
        raise GitError('This remote has multiple push addresses. Choose one in Git first.')
    code, merge = _run(path, ['config', '--get', f'branch.{branch}.merge'], check=False)
    destination = merge.strip().removeprefix('refs/heads/') if not code else branch
    validate_branch(path, destination)
    return dict(remote=remote, url=urls[0], branch=branch, destination=destination,
                head=rev_parse(path))


def push(path, target):
    try:
        return _run(path, ['push', '--set-upstream', '--', target['remote'],
                          f"refs/heads/{target['branch']}:refs/heads/{target['destination']}"], timeout=120, hooks=True)
    except GitError as error:
        raise GitError('Push failed. Check your Git sign-in and remote. If rejected, pull first. ' + str(error)) from error


def pull(path):
    if run_git(path, 'status')[1]:
        raise GitError('Commit or discard your changes before pulling.')
    if not session_status(path)['upstream']:
        raise GitError('Push this branch once to set its upstream before pulling.')
    try:
        return _run(path, ['pull', '--ff-only'], timeout=120, hooks=True)
    except GitError as error:
        raise GitError('Pull could not fast-forward. Check your Git sign-in, or resolve diverged branches in Git. ' + str(error)) from error


def merged(path, branch, base):
    return _run(path, ['merge-base', '--is-ancestor', rev_parse(path, branch), rev_parse(path, base)], check=False)[0] == 0


def discard_branch(repo, branch, base):
    validate_branch(repo, branch)
    current = run_git(repo, 'branch', check=False)[1].strip()
    if branch in (base, current):
        return False
    _run(repo, ['branch', '-D', '--', branch])
    return True


def merge_back(repo, branch, base):
    validate_branch(repo, branch); validate_branch(repo, base)
    if run_git(repo, 'branch', check=False)[1].strip() != base:
        raise GitError(f'Open {base} in the main project folder before merging back.')
    if run_git(repo, 'status')[1]:
        raise GitError('The main project folder has uncommitted changes. Commit or discard them before merging back.')
    # Refuse an existing operation; never abort a merge belonging to the person.
    if _run(repo, ['rev-parse', '--verify', 'MERGE_HEAD'], check=False)[0] == 0:
        raise GitError('Finish the existing merge in the main project folder first.')
    try:
        return _run(repo, ['merge', '--no-edit', branch], timeout=120, hooks=True)
    except GitError as error:
        conflicts = _run(repo, ['diff', '--name-only', '--diff-filter=U', '-z', '--'])[1].split('\0')
        if _run(repo, ['rev-parse', '--verify', 'MERGE_HEAD'], check=False)[0] == 0:
            _run(repo, ['merge', '--abort'])
        names = ', '.join(name for name in conflicts if name)
        raise GitError(('Merge was cancelled. Conflicted files: ' + names + '. ') if names else 'Merge failed. No merge was kept. ' + str(error)) from error


def _commits(text):
    commits = []
    for record in text.split('\0COMMIT\0')[1:]:
        parts = record.split('\0', 5)
        if len(parts) != 6:
            raise GitError('Git history could not be read.')
        sha, stamp, name, email, subject, stats = parts
        files = []
        for line in stats.split('\0'):
            fields = line.lstrip('\n').split('\t', 2)
            if len(fields) == 3:
                added, removed, filename = fields
                files.append((filename, int(added) if added.isdigit() else 0,
                              int(removed) if removed.isdigit() else 0))
        commits.append(dict(sha=sha, at=int(stamp), name=name, email=email, subject=subject, files=files))
    return commits


def activity_days(now=None):
    today = (now or datetime.now().astimezone()).date()
    return [dict(date=(today - timedelta(days=i)).isoformat(), commits=0, added=0, removed=0)
            for i in range(13, -1, -1)]


def _summarize(path, commits, now):
    activity = activity_days(now)
    days = {d['date']: d for d in activity}
    rhythm = [[0] * 24 for _ in range(7)]
    authors, hotspots, buckets = {}, {}, Counter()
    first = min((c['at'] for c in commits), default=None)
    last = max((c['at'] for c in commits), default=None)
    age = max(0, (now.date() - datetime.fromtimestamp(first).date()).days) if first is not None else 0
    monthly = age > 365
    windows = [dict(commits=0, authors=set(), added=0, removed=0) for _ in range(2)]
    added = removed = 0
    for c in commits:
        date = datetime.fromtimestamp(c['at']).astimezone()
        a = sum(f[1] for f in c['files']); r = sum(f[2] for f in c['files'])
        added += a; removed += r
        rhythm[date.weekday()][date.hour] += 1
        key = date.strftime('%Y-%m') if monthly else (date.date() - timedelta(days=date.weekday())).isoformat()
        buckets[key] += 1
        author = authors.setdefault(c['email'], dict(name=c['name'], commits=0, added=0, removed=0))
        author['commits'] += 1; author['added'] += a; author['removed'] += r
        if date.date().isoformat() in days:
            day = days[date.date().isoformat()]
            day['commits'] += 1; day['added'] += a; day['removed'] += r
        elapsed = (now.timestamp() - c['at']) / 86400
        if 0 <= elapsed < 60:
            w = windows[0 if elapsed < 30 else 1]
            w['commits'] += 1; w['authors'].add(c['email']); w['added'] += a; w['removed'] += r
        for filename, a, r in c['files']:
            file = hotspots.setdefault(filename, dict(path=filename, commits=0, added=0, removed=0))
            file['commits'] += 1; file['added'] += a; file['removed'] += r
    # Include quiet periods so the lifespan chart preserves elapsed time.
    if first is not None:
        cursor = datetime.fromtimestamp(first).date()
        cursor = cursor.replace(day=1) if monthly else cursor - timedelta(days=cursor.weekday())
        while cursor <= now.date():
            key = cursor.strftime('%Y-%m') if monthly else cursor.isoformat()
            buckets.setdefault(key, 0)
            cursor = (cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1)) if monthly else cursor + timedelta(days=7)
    languages = defaultdict(int)
    root = Path(path)
    for name in run_git(path, 'files')[1].split('\0'):
        if not name:
            continue
        file = root / name
        # Never follow a tracked symlink out of the project.
        if file.is_symlink() or not file.resolve().is_relative_to(root):
            continue
        try:
            languages[file.suffix.lower() or 'No extension'] += file.stat().st_size
        except OSError:
            continue
    ranked = sorted(languages.items(), key=lambda row: (-row[1], row[0]))
    lang = [dict(name=k, bytes=v) for k, v in ranked[:6]]
    if len(ranked) > 6:
        lang.append(dict(name='Other', bytes=sum(v for _, v in ranked[6:])))
    delta = {k: (len(windows[0]['authors']) - len(windows[1]['authors']) if k == 'contributors'
                 else windows[0][k] - windows[1][k]) for k in ('commits', 'contributors', 'added', 'removed')}
    delta['age_days'] = min(age, 30)
    return dict(activity=activity, lifespan=dict(commits=len(commits), contributors=len(authors),
                first_commit=first, last_commit=last, added=added, removed=removed, age_days=age,
                delta=delta, bucket='monthly' if monthly else 'weekly',
                buckets=[dict(date=k, commits=v) for k, v in sorted(buckets.items())]),
                languages=lang, rhythm=rhythm,
                hotspots=sorted(hotspots.values(), key=lambda f: (-f['commits'], f['path']))[:10],
                contributors=sorted(authors.values(), key=lambda a: (-a['commits'], a['name']))[:10])


def summary(project, now=None):
    path = vet_workspace(project['path'])
    if not path:
        return dict(state='unavailable', message='Project folder is unavailable.')
    code, root = run_git(path, 'repo', check=False)
    if code or os.path.normcase(root.strip()) != os.path.normcase(path):
        return dict(state='not_git', message='Not a git repository')
    head_code, head = run_git(path, 'head', check=False)
    head = head.strip() if not head_code else ''
    index = Path(run_git(path, 'index')[1].strip())
    if not index.is_absolute():
        index = Path(path) / index
    index_stamp = index.stat().st_mtime_ns if index.exists() else 0
    now = now or datetime.now().astimezone()
    # Time-sensitive windows advance even while HEAD stays put.
    key = (project['id'], path, head, index_stamp, now.date().isoformat())
    with _cache_lock:
        result = _cache.get(key)
        if result is not None:
            _cache.move_to_end(key)
            return copy.deepcopy(result)
    commits = _commits(run_git(path, 'log')[1]) if head else []
    result = _summarize(path, commits, now)
    result.update(state='ok' if commits else 'empty', head=head or None,
                  branch=run_git(path, 'branch', check=False)[1].strip() or 'Detached HEAD',
                  last_commit=next((dict(sha=c['sha'], at=c['at'], subject=c['subject']) for c in commits), None))
    with _cache_lock:
        # Keep one revision per project, with a bounded number of projects.
        for old in list(_cache):
            if old[0] == project['id']:
                del _cache[old]
        _cache[key] = copy.deepcopy(result)
        while len(_cache) > 64:
            _cache.popitem(last=False)
    return result
