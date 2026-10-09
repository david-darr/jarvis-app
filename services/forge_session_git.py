"""Person-only Git actions and approvals for Forge sessions."""
from core import permissions
from services import forge_git
from services.forge_sessions import forge_sessions, confined, ReviewConflict

CHOICES = [{'id': 'once', 'label': 'Approve', 'behavior': 'allow', 'scope': 'once'},
           {'id': 'reject', 'label': 'Cancel', 'behavior': 'deny', 'scope': 'once'}]


class ForgeSessionGit:
    def __init__(self, sessions=None):
        self.sessions = sessions or forge_sessions

    def first_commit_target(self, project_id):
        project, repo = self.sessions.project(project_id)
        if forge_git.has_commits(repo, any_branch=True):
            raise ValueError('This project already has commits.')
        status = forge_git.run_git(repo, 'status')[1]
        count = len(forge_git.status_files(status))
        if not count:
            raise ValueError('There are no files to commit.')
        return dict(project_id=project_id, name=project['name'], folder=str(repo),
                    files=count, status=status)

    async def first_commit(self, project_id, surface):
        import asyncio
        with self.sessions.lock:
            target = self.first_commit_target(project_id)
        description = (f"Make the first commit for {target['name']} as Initial commit.\n"
                       f"Project folder: {target['folder']}\n"
                       f"Files to commit: {target['files']}\nFiles ignored by Git are excluded.")
        decision = await permissions.decide(surface=surface, tool='forge_git_first_commit',
            arguments=target, title='Make the first commit?', description=description,
            target=description, is_admin=True, force_prompt=True, choices=CHOICES)
        if decision.behavior != 'allow':
            raise ValueError('The first commit was not approved.')

        def execute():
            with self.sessions.lock:
                if self.first_commit_target(project_id) != target:
                    raise ReviewConflict('The project or files changed during approval. Try again.')
                forge_git.initial_commit(target['folder'])
                return {'ok': True}
        return await asyncio.to_thread(execute)

    def status(self, session_id):
        with self.sessions.lock:
            root = self.sessions.workspace(session_id)
            result = forge_git.session_status(root)
            _, repo = self.sessions.project(self.sessions.get(session_id)['forge']['project_id'])
            base = self.sessions.get(session_id)['forge']['base_branch']
            result['unmerged'] = not forge_git.merged(repo, result['branch'], base) if result['branch'] != 'Detached HEAD' else True
            return result

    def action(self, session_id, action, path=None, message='', branch=''):
        with self.sessions.lock:
            root = self.sessions.workspace(session_id)
            if action in ('stage', 'unstage'):
                if path is not None:
                    confined(root, path)
                    rows = forge_git.session_status(root)['files']
                    if path not in [row['path'] for row in rows]:
                        raise ValueError('Choose a changed file.')
                forge_git.stage(root, path, action == 'unstage')
            elif action == 'commit':
                forge_git.commit(root, message)
            elif action in ('create-branch', 'switch'):
                forge_git.switch_branch(root, branch, action == 'create-branch')
                details = self.sessions.get(session_id)['forge']
                self.sessions.sessions.set_forge(session_id, {**details, 'branch': branch})
            elif action == 'pull':
                forge_git.pull(root)
            else:
                raise ValueError('Choose a Git action.')
            return self.status(session_id)

    def merge_target(self, session_id):
        root = self.sessions.workspace(session_id)
        details = self.sessions.get(session_id)['forge']
        _, repo = self.sessions.project(details['project_id'])
        branch = forge_git.run_git(root, 'branch', check=False)[1].strip()
        if not branch or branch == details['base_branch']:
            raise ValueError('Choose a session branch different from the base branch before merging back.')
        if forge_git.run_git(root, 'status')[1]:
            raise ValueError('Commit your session changes before merging back.')
        if forge_git.run_git(repo, 'status')[1]:
            raise ValueError('The main project folder has uncommitted changes. Commit or discard them before merging back.')
        if forge_git.run_git(repo, 'branch', check=False)[1].strip() != details['base_branch']:
            raise ValueError(f"Open {details['base_branch']} in the main project folder before merging back.")
        return dict(root=str(root), repo=str(repo), branch=branch, base=details['base_branch'],
                    head=forge_git.rev_parse(root), base_head=forge_git.rev_parse(repo, details['base_branch']))

    async def approved(self, session_id, action, surface, remove_after=False):
        import asyncio
        from services import chat_service
        with self.sessions.lock:
            root = self.sessions.workspace(session_id)
            target = forge_git.push_target(root) if action == 'push' else self.merge_target(session_id)
        description = (f"Push {target['branch']} to {target['remote']} ({target['url']}), branch {target['destination']}."
                       if action == 'push' else f"Merge {target['branch']} into {target['base']}.\nMain project folder: {target['repo']}")
        decision = await permissions.decide(surface=surface, tool='forge_git_' + action,
            arguments=target, title='Push branch?' if action == 'push' else 'Merge back?',
            description=description, target=description, is_admin=True, force_prompt=True, choices=CHOICES)
        if decision.behavior != 'allow':
            raise ValueError(decision.reason or 'Git action was not approved.')
        async with chat_service.session_operation(session_id):
            def execute():
                with self.sessions.lock:
                    current_root = self.sessions.workspace(session_id)
                    current = forge_git.push_target(current_root) if action == 'push' else self.merge_target(session_id)
                    if current_root != root or current != target:
                        raise ReviewConflict('The branch or destination changed during approval. Try again.')
                    if action == 'push':
                        forge_git.push(root, target)
                    else:
                        forge_git.merge_back(target['repo'], target['branch'], target['base'])
            await asyncio.to_thread(execute)
            if remove_after:
                result = await asyncio.to_thread(self.sessions.remove, session_id)
                await chat_service.close_session_brain(session_id)
                return result
            return await asyncio.to_thread(self.status, session_id)

    def end(self, session_id, option, confirmed=False):
        if option not in ('keep-branch', 'discard'):
            raise ValueError('Choose how to end this session.')
        state = self.status(session_id)
        if option == 'discard' and (state['files'] or state['unmerged']) and not confirmed:
            raise ReviewConflict('This session has uncommitted or unmerged changes. Confirm discarding it first.')
        details = self.sessions.get(session_id)['forge']
        _, repo = self.sessions.project(details['project_id'])
        result = self.sessions.remove(session_id, option == 'discard', option == 'discard')
        if option == 'discard' and details['isolation'] != 'in_place':
            try:
                result['branch_discarded'] = forge_git.discard_branch(repo, state['branch'], details['base_branch'])
            except forge_git.GitError as error:
                result['warning'] = 'Worktree removed, but the branch was kept. ' + str(error)
        return result


forge_session_git = ForgeSessionGit()
