"""Read-only Explorer for registered folders before a session is selected."""
from pathlib import Path

from services import forge_git
from services.forge_projects import vet_project
from services.forge_sessions import confined, ForgeSessions


def project_files(project, name=''):
    root = Path(vet_project(project['path']))
    folder = confined(root, name, directory=True)
    if not folder.is_dir():
        raise ValueError('Directory is unavailable.')
    code, tracked = forge_git.run_git(root, 'files', check=False)
    if code == 0:
        names = tracked.split('\0') + forge_git.run_git(root, 'untracked')[1].split('\0')
        prefix = name + '/' if name else ''
        children = {prefix + path[len(prefix):].split('/', 1)[0]
                    for path in names if path and path.startswith(prefix)}
    else:
        children = set()
        for child in folder.iterdir():
            children.add('/'.join(filter(None, (name, child.name))))
            if len(children) >= 500:
                break
    entries = []
    for child in children:
        try:
            path = confined(root, child)
            if not path.exists():
                continue
            entries.append(dict(name=path.name, path=child, directory=path.is_dir(), changed=False))
        except ValueError:
            continue
    entries.sort(key=lambda entry: (not entry['directory'], entry['name'].casefold()))
    return dict(path=name, entries=entries[:500], truncated=len(children) >= 500)


def project_file(project, name):
    root = Path(vet_project(project['path']))
    return dict(path=name, content=ForgeSessions._text(confined(root, name)))
