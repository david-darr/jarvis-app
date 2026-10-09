"""Forge Preview: local projects and git summaries, admin only."""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.middleware import require_admin
from services import forge_git
from services.forge_projects import forge_projects, forge_root, set_root, vet_project

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
        matching = next((p for p in rows if os.path.normcase(p['path']) == os.path.normcase(full.get('workspace_dir') or '')), None)
        if matching and chat_service.is_busy(s['id']):
            working.append(dict(id=s['id'], title=s['title'], project_name=matching['name']))
    return dict(days=list(days.values()), projects=states, working=working)
