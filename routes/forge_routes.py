"""Forge Preview: local projects and git summaries, admin only."""
import os

from fastapi import APIRouter, Depends, HTTPException
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
                            body.mode, body.isolation, body.branch, body.base_branch, user))


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
