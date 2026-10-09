"""Bounded, non-interactive git reads for Forge Preview. No shell or git writes."""
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
    env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'GCM_', 'SSH_ASKPASS'))}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_ASKPASS=os.devnull,
               SSH_ASKPASS=os.devnull, GCM_INTERACTIVE='Never',
               GIT_SSH_COMMAND='ssh -oBatchMode=yes -oStrictHostKeyChecking=yes',
               GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    # The null device cannot execute: askpass always fails without a UI.
    # GIT_TERMINAL_PROMPT=0 also disables its terminal fallback.
    return env


def _run(cwd, args, timeout=TIMEOUT, output_cap=OUTPUT_CAP, check=True):
    folder = vet_workspace(str(cwd))
    if not folder:
        raise GitError('Project folder is unavailable or unusable.')
    command = ['git', '--no-pager', '-c', 'core.hooksPath=' + os.devnull,
               '-c', 'credential.interactive=false', '-c', 'core.askPass=' + os.devnull,
               '-c', 'core.fsmonitor=false',
               '-c', 'protocol.ext.allow=never', *args]
    output = bytearray()
    overflow = threading.Event()
    try:
        with subprocess.Popen(command, cwd=folder, env=git_env(), stdin=subprocess.DEVNULL,
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
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                reader.join(timeout=1)
                raise GitError('Git timed out. Try again with a smaller repository.')
            reader.join(timeout=1)
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
    'files': ['ls-files', '-z', '--'],
    'log': ['log', '--no-ext-diff', '--no-textconv', '--no-renames', '--format=%x00COMMIT%x00%H%x00%at%x00%aN%x00%aE%x00%s%x00', '--numstat', '-z', 'HEAD', '--'],
}


def run_git(path, recipe, **options):
    if recipe not in READS:
        raise GitError('Forge Preview supports read-only git summaries.')
    return _run(path, READS[recipe], **options)


def init_repo(path):
    return _run(path, ['init', '--', str(Path(path).resolve())])


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
