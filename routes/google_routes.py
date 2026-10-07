"""Admin-owned Google Workspace connection and Library actions."""
from __future__ import annotations

import html
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from core import google_workspace as google
from core.middleware import local_api_base, require_admin


router = APIRouter(prefix="/api/google", tags=["google-workspace"])
CALLBACK = "/google/oauth/callback"


class ClientRequest(BaseModel):
    client_id: str
    client_secret: str = ""


class DriveActionRequest(BaseModel):
    action: Literal["create_folder", "rename", "move", "copy", "trash", "restore", "star", "unstar", "delete", "permissions", "share", "update_permission", "revoke", "revisions"]
    file_id: str | None = None
    name: str | None = None
    parent: str | None = None
    permission_id: str | None = None
    email: str | None = None
    role: str | None = None
    permission_type: str | None = None
    domain: str | None = None


class SheetActionRequest(BaseModel):
    action: Literal["create", "update", "append", "clear", "batch"]
    file_id: str | None = None
    title: str | None = None
    cell_range: str | None = None
    values: list | None = None
    requests: list | None = None


class FormActionRequest(BaseModel):
    action: Literal["create", "batch", "publish"]
    file_id: str | None = None
    title: str | None = None
    requests: list | None = None
    published: bool | None = None


async def _run(coroutine):
    try:
        return await coroutine
    except google.GoogleError as problem:
        raise HTTPException(problem.status, str(problem)) from problem


@router.get("/status")
async def status(user: str = Depends(require_admin)) -> dict:
    return google.status()


@router.post("/client")
async def configure(body: ClientRequest, user: str = Depends(require_admin)) -> dict:
    try:
        return google.configure(body.client_id, body.client_secret)
    except google.GoogleError as problem:
        raise HTTPException(problem.status, str(problem)) from problem


@router.delete("/connection")
async def disconnect(user: str = Depends(require_admin)) -> dict:
    return google.disconnect()


@router.post("/oauth/start")
async def start(user: str = Depends(require_admin)) -> dict:
    try:
        return {"url": google.start_sign_in(local_api_base() + CALLBACK)}
    except google.GoogleError as problem:
        raise HTTPException(problem.status, str(problem)) from problem


@router.get("/oauth/callback", response_class=HTMLResponse)
async def callback(state: str = "", code: str | None = None, error: str | None = None) -> HTMLResponse:
    try:
        account = await google.finish_sign_in(state, code, local_api_base() + CALLBACK, error)
        message, status_code = f"Connected {html.escape(account)}. Return to Kairos.", 200
    except google.GoogleError as problem:
        message, status_code = html.escape(str(problem)), problem.status
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Kairos Google connection</title>"
                        f"<body style='font-family:system-ui;max-width:34rem;margin:4rem auto;padding:1rem'>"
                        f"<h1>Google Workspace</h1><p>{message}</p></body>", status_code=status_code)


@router.get("/drive/files")
async def files(q: str = "", parent: str | None = None, kind: str = "all", trashed: bool = False,
                page_token: str | None = None, user: str = Depends(require_admin)) -> dict:
    return await _run(google.list_files(query=q, parent=parent, kind=kind, trashed=trashed, page_token=page_token))


@router.get("/drive/files/{file_id}")
async def file_info(file_id: str, user: str = Depends(require_admin)) -> dict:
    return await _run(google.file_info(file_id))


_EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


@router.get("/drive/files/{file_id}/download")
async def download(file_id: str, user: str = Depends(require_admin)) -> Response:
    info = await _run(google.file_info(file_id))
    mime = info.get("mimeType") or "application/octet-stream"
    if mime.startswith("application/vnd.google-apps.") and mime not in _EXPORTS:
        raise HTTPException(400, "This Google file cannot be exported; open it in Google instead")
    export_mime, suffix = _EXPORTS.get(mime, (None, ""))
    data = await _run(google.file_content(file_id, export_mime))
    filename = (info.get("name") or "download") + suffix
    return Response(content=data, media_type=export_mime or mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


@router.post("/drive/upload")
async def upload(file: UploadFile = File(...), parent: str | None = Form(None),
                 user: str = Depends(require_admin)) -> dict:
    data = await file.read(google.MAX_DOWNLOAD + 1)
    return await _run(google.upload_file(file.filename or "upload", data,
                                          file.content_type or "application/octet-stream", parent))


@router.post("/drive/action")
async def drive_action(body: DriveActionRequest, user: str = Depends(require_admin)) -> dict:
    return await _run(google.drive_action(**body.model_dump()))


@router.get("/sheets/{file_id}")
async def sheet(file_id: str, cell_range: str | None = None,
                user: str = Depends(require_admin)) -> dict:
    return await _run(google.sheet_get(file_id, cell_range))


@router.post("/sheets/action")
async def sheet_action(body: SheetActionRequest, user: str = Depends(require_admin)) -> dict:
    return await _run(google.sheet_action(**body.model_dump()))


@router.get("/forms/{file_id}")
async def form(file_id: str, responses: bool = False, page_token: str | None = None,
               user: str = Depends(require_admin)) -> dict:
    return await _run(google.form_get(file_id, responses, page_token))


@router.post("/forms/action")
async def form_action(body: FormActionRequest, user: str = Depends(require_admin)) -> dict:
    return await _run(google.form_action(**body.model_dump()))
