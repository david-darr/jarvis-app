"""Forge Preview: local projects and git summaries, admin only."""
import os
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictBool
from typing import Literal

from core.middleware import require_admin
from services import forge_git
from services.forge_projects import forge_projects, forge_root, set_root, vet_project
from services.forge_sessions import forge_sessions, ReviewConflict
from core import file_checkpoints
from services import chat_service
from services.forge_project_files import project_files, project_file
from routes.session_routes import _for_client
from services.forge_apps import forge_apps
from core import permissions
from services.forge_session_git import forge_session_git
from services.forge_terminals import forge_terminals
from core import settings as settings_store

router = APIRouter(prefix='/api/forge', tags=['forge'], dependencies=[Depends(require_admin)])


class ExistingRequest(BaseModel):
    path: str
    name: str = ''


class NewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CloneRequest(BaseModel):
    url: str
    name: str = ''


class RootRequest(BaseModel):
    path: str


def call(fn, *args):
    try:
        return fn(*args)
    except (ReviewConflict, file_checkpoints.Conflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def project(project_id):
    item = forge_projects.get(project_id)
    if not item:
        raise HTTPException(status_code=404, detail='Project not found.')
    call(vet_project, item['path'])
    return item


def read_summary(item):
    try:
        vet_project(item['path'])
        return forge_git.summary(item)
    except (ValueError, OSError) as error:
        return dict(state='error', message=str(error))


@router.get('/root')
def root():
    return {'path': str(call(forge_root))}


@router.put('/root')
def update_root(body: RootRequest):
    return {'path': call(set_root, body.path)}


@router.get('/projects')
def projects():
    return [{**p, 'git': read_summary(p)} for p in forge_projects.list()]


@router.post('/projects')
def add_existing(body: ExistingRequest):
    return call(forge_projects.add, body.path, body.name)


@router.post('/projects/new')
def new_project(body: NewRequest):
    return call(forge_projects.new, body.name)


@router.post('/projects/clone')
def clone_project(body: CloneRequest):
    return call(forge_projects.clone, body.url, body.name)


@router.get('/workspaces/recent')
def recent_workspaces():
    return forge_projects.recent_workspaces()


@router.get('/projects/{project_id}/summary')
def summary(project_id: str):
    return read_summary(project(project_id))


@router.get('/projects/{project_id}/files')
def read_project_files(project_id: str, path: str = ''):
    return call(project_files, project(project_id), path)


@router.get('/projects/{project_id}/file')
def read_project_file(project_id: str, path: str):
    return call(project_file, project(project_id), path)


@router.post('/projects/{project_id}/opened')
def opened(project_id: str):
    project(project_id)
    return call(forge_projects.opened, project_id)


@router.delete('/projects/{project_id}')
def remove(project_id: str):
    forge_projects.remove(project_id)
    return {'ok': True}


@router.get('/activity')
def activity():
    from core.session_manager import session_manager
    from services import chat_service
    rows = forge_projects.list()
    days = {d['date']: d for d in forge_git.activity_days()}
    states = []
    for p in rows:
        result = read_summary(p)
        states.append(dict(id=p['id'], name=p['name'], state=result['state'], message=result.get('message')))
        for day in result.get('activity', []):
            if day['date'] in days:
                for key in ('commits', 'added', 'removed'):
                    days[day['date']][key] += day[key]
    working = []
    for s in session_manager.list_sessions(include_agents=True):
        full = session_manager.get_session(s['id']) or {}
        matching = next((p for p in rows if p['id'] == (full.get('forge') or {}).get('project_id')
                         or os.path.normcase(p['path']) == os.path.normcase(full.get('workspace_dir') or '')), None)
        if matching and chat_service.is_busy(s['id']):
            working.append(dict(id=s['id'], title=s['title'], project_name=matching['name']))
    return dict(days=list(days.values()), projects=states, working=working)


class CreateForgeSessionRequest(BaseModel):
    project_id: str
    task: str = Field(min_length=1, max_length=4000)
    model_endpoint_id: str | None = None
    model_override: str | None = Field(default=None, max_length=160)
    mode: Literal['build', 'plan'] = 'build'
    isolation: Literal['new_worktree', 'existing_branch', 'in_place'] = 'new_worktree'
    branch: str | None = None
    base_branch: str | None = None


class ModeRequest(BaseModel):
    mode: Literal['build', 'plan']


class ConfirmRequest(BaseModel):
    confirmed: StrictBool = False


class RevertFileRequest(ConfirmRequest):
    path: str


class RevertHunkRequest(RevertFileRequest):
    hunk_hash: str


@router.post('/sessions')
def create_forge_session(body: CreateForgeSessionRequest, user: str = Depends(require_admin)):
    return _for_client(call(forge_sessions.create, body.project_id, body.task, body.model_endpoint_id,
                            body.mode, body.isolation, body.branch, body.base_branch, user, body.model_override))


@router.get('/projects/{project_id}/sessions')
def project_sessions(project_id: str):
    return call(forge_sessions.list, project_id)


@router.get('/projects/{project_id}/branches')
def project_branches(project_id: str):
    return call(forge_sessions.branches, project_id)


@router.get('/sessions/{session_id}')
def get_forge_session(session_id: str):
    return _for_client(call(forge_sessions.get, session_id))


@router.post('/sessions/{session_id}/mode')
async def session_mode(session_id: str, body: ModeRequest):
    call(forge_sessions.get, session_id)
    async with chat_service.session_operation(session_id):
        await chat_service.close_session_brain(session_id)
        return _for_client(call(forge_sessions.set_mode, session_id, body.mode))


@router.get('/sessions/{session_id}/changes')
def session_changes(session_id: str):
    return call(forge_sessions.changes, session_id)


@router.post('/sessions/{session_id}/revert-hunk')
async def revert_hunk(session_id: str, body: RevertHunkRequest):
    call(forge_sessions.get, session_id)
    async with chat_service.session_operation(session_id):
        return call(forge_sessions.revert_hunk, session_id, body.path, body.hunk_hash, body.confirmed)


@router.post('/sessions/{session_id}/revert-file')
async def revert_file(session_id: str, body: RevertFileRequest):
    call(forge_sessions.get, session_id)
    async with chat_service.session_operation(session_id):
        return call(forge_sessions.revert_file, session_id, body.path, body.confirmed)


@router.get('/sessions/{session_id}/checkpoints')
def session_checkpoints(session_id: str):
    return call(forge_sessions.checkpoints, session_id)


@router.post('/sessions/{session_id}/checkpoints/{event_id}/undo')
async def undo_checkpoint(session_id: str, event_id: str, body: ConfirmRequest):
    call(forge_sessions.get, session_id)
    async with chat_service.session_operation(session_id):
        return call(forge_sessions.undo, session_id, event_id, body.confirmed)


@router.delete('/sessions/{session_id}')
async def remove_forge_session(session_id: str, discard: bool = False, confirmed: bool = False):
    call(forge_sessions.get, session_id)
    async with chat_service.session_operation(session_id):
        result = call(forge_sessions.remove, session_id, discard, confirmed)
        await chat_service.close_session_brain(session_id)
        return result


@router.get('/sessions/{session_id}/files')
def session_files(session_id: str, path: str = ''):
    return call(forge_sessions.files, session_id, path)


@router.get('/sessions/{session_id}/file')
def session_file(session_id: str, path: str):
    return call(forge_sessions.file, session_id, path)


class SaveFileRequest(BaseModel):
    path: str
    content: str = Field(max_length=1024 * 1024)
    hash: str
    mtime: str
    overwrite: StrictBool = False


@router.put('/sessions/{session_id}/file')
def save_session_file(session_id: str, body: SaveFileRequest):
    return call(forge_sessions.save_file, session_id, body.path, body.content,
                body.hash, body.mtime, body.overwrite)


class AppCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)


@router.get('/sessions/{session_id}/app/suggest')
def app_suggest(session_id: str):
    return call(forge_apps.suggest, session_id)


@router.put('/projects/{project_id}/app/command')
def app_command(project_id: str, body: AppCommandRequest):
    return call(forge_apps.set_command, project_id, body.command)


def approval_stream(surface, launch):
    async def stream():
        # One stream owns the approval channel; duplicate launches cannot replace it.
        if surface in permissions._channels:
            yield 'data: ' + json.dumps({'error': 'Another action is already waiting for approval.'}) + '\n\n'
            return
        queue = permissions.open_channel(surface)
        async def run():
            try:
                result = await launch(surface)
                await queue.put({'status': result})
            except (ValueError, OSError, KeyError, HTTPException) as error:
                await queue.put({'error': str(error.detail) if isinstance(error, HTTPException) else str(error)})
        task = asyncio.create_task(run())
        try:
            while True:
                packet = await queue.get()
                terminal = 'status' in packet or 'error' in packet
                yield 'data: ' + json.dumps(packet if terminal else {'permission': packet}) + '\n\n'
                if terminal:
                    break
        finally:
            permissions.close_channel(surface)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store'})


def app_start_stream(session_id, restart=False):
    call(forge_sessions.workspace, session_id)
    return approval_stream(f'forge-app:{session_id}',
        lambda surface: (forge_apps.restart if restart else forge_apps.start)(session_id, True, surface))


@router.post('/sessions/{session_id}/app/start')
async def app_start(session_id: str):
    return app_start_stream(session_id)


@router.post('/sessions/{session_id}/app/restart')
async def app_restart(session_id: str):
    return app_start_stream(session_id, True)


@router.post('/sessions/{session_id}/app/stop')
def app_stop(session_id: str):
    return call(forge_apps.stop, session_id)


@router.get('/sessions/{session_id}/app/status')
def app_status(session_id: str):
    return call(forge_apps.status, session_id)


@router.get('/sessions/{session_id}/app/logs')
def app_logs(session_id: str, limit: int = 200):
    return call(forge_apps.logs, session_id, limit)


@router.get('/sessions/{session_id}/app/allowed-ports')
def app_allowed_ports(session_id: str):
    return {'ports': call(forge_apps.status, session_id)['ports']}


@router.get('/apps/running')
def running_apps():
    return forge_apps.running()


@router.post('/apps/shutdown')
def shutdown_apps():
    try:
        forge_apps.shutdown()
    finally:
        forge_terminals.shutdown()
    return {'ok': True}


class EndSessionRequest(BaseModel):
    option: Literal['leave', 'merge', 'keep-branch', 'discard'] = 'leave'
    confirmed: StrictBool = False


@router.post('/sessions/{session_id}/end')
async def end_session(session_id: str, body: EndSessionRequest = EndSessionRequest()):
    call(forge_sessions.get, session_id)
    if body.option == 'merge':
        call(forge_sessions.workspace, session_id)
        return approval_stream(f'forge-git:{session_id}', lambda surface: forge_session_git.approved(session_id, 'merge', surface, True))
    async with chat_service.session_operation(session_id):
        result = call(forge_apps.stop, session_id) if body.option == 'leave' else call(forge_session_git.end, session_id, body.option, body.confirmed)
        forge_terminals.stop_session(session_id)
        await chat_service.close_session_brain(session_id)
        return result


class GitActionRequest(BaseModel):
    action: Literal['stage', 'unstage', 'commit', 'create-branch', 'switch', 'pull', 'push', 'merge']
    path: str | None = None
    message: str = Field(default='', max_length=4000)
    branch: str = Field(default='', max_length=250)


@router.get('/sessions/{session_id}/git')
def git_status(session_id: str):
    return call(forge_session_git.status, session_id)


@router.post('/sessions/{session_id}/git')
async def git_action(session_id: str, body: GitActionRequest):
    call(forge_sessions.workspace, session_id)
    if body.action in ('push', 'merge'):
        return approval_stream(f'forge-git:{session_id}', lambda surface: forge_session_git.approved(session_id, body.action, surface))
    async with chat_service.session_operation(session_id):
        return await asyncio.to_thread(call, forge_session_git.action, session_id, body.action, body.path, body.message, body.branch)


def terminal_access(request: Request, user: str = Depends(require_admin)):
    from services.forge_terminal_access import local_terminal_request
    if not local_terminal_request(request.scope) and not settings_store.get_setting('forge_terminal_remote'):
        raise HTTPException(status_code=403, detail='The terminal is off over Remote Access. Turn on Allow the terminal over Remote Access in Settings > Forge to use it here.')
    return user


class TerminalSizeRequest(BaseModel):
    rows: int = Field(default=24, ge=2, le=300, strict=True)
    cols: int = Field(default=80, ge=2, le=500, strict=True)


class TerminalInputRequest(BaseModel):
    data: str = Field(max_length=65536)


@router.post('/sessions/{session_id}/terminals', dependencies=[Depends(terminal_access)])
def terminal_start(session_id: str, body: TerminalSizeRequest, request: Request):
    from services.forge_terminal_access import local_terminal_request
    return call(forge_terminals.start, session_id, body.rows, body.cols, True, not local_terminal_request(request.scope))


@router.get('/sessions/{session_id}/terminals/{terminal_id}/output', dependencies=[Depends(terminal_access)])
async def terminal_output(session_id: str, terminal_id: str, request: Request, after: int = 0):
    try:
        cursor = max(0, int(request.headers.get('last-event-id') or after))
    except ValueError:
        raise HTTPException(status_code=400, detail='Invalid terminal output cursor.')
    call(forge_terminals.output, session_id, terminal_id, cursor)
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            # Check the setting on reconnect and during a long-lived remote stream.
            terminal_access(request, '')
            try:
                packet = forge_terminals.output(session_id, terminal_id, cursor)
            except (KeyError, ValueError):
                yield 'event: closed\ndata: {}\n\n'; break
            if packet['reset']:
                yield 'event: reset\ndata: {}\n\n'
            for event in packet['events']:
                cursor = event['id']
                yield f"id: {cursor}\ndata: " + json.dumps({'output': event['data']}) + '\n\n'
            if packet['ended']:
                yield 'event: closed\ndata: {}\n\n'; break
            if not packet['events']:
                yield ': keepalive\n\n'
            await asyncio.sleep(.1)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})


@router.post('/sessions/{session_id}/terminals/{terminal_id}/input', dependencies=[Depends(terminal_access)])
def terminal_input(session_id: str, terminal_id: str, body: TerminalInputRequest):
    return call(forge_terminals.write, session_id, terminal_id, body.data)


@router.post('/sessions/{session_id}/terminals/{terminal_id}/resize', dependencies=[Depends(terminal_access)])
def terminal_resize(session_id: str, terminal_id: str, body: TerminalSizeRequest):
    return call(forge_terminals.resize, session_id, terminal_id, body.rows, body.cols)


@router.delete('/sessions/{session_id}/terminals/{terminal_id}', dependencies=[Depends(terminal_access)])
def terminal_close(session_id: str, terminal_id: str):
    return call(forge_terminals.close, session_id, terminal_id)


class TerminalRemoteRequest(BaseModel):
    enabled: StrictBool


@router.get('/terminal/settings')
def terminal_settings():
    return {'forge_terminal_remote': bool(settings_store.get_setting('forge_terminal_remote'))}


@router.put('/terminal/settings')
def save_terminal_settings(body: TerminalRemoteRequest):
    settings_store.update_settings(forge_terminal_remote=body.enabled)
    if not body.enabled:
        forge_terminals.stop_remote()
    return terminal_settings()
