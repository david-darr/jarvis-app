"""Answering, listing and revoking permission requests.

Answering is a normal user action - it is the person's own prompt. Revoking a
saved rule is admin-gated like the other settings that change what models may
do, because a rule is global to this install rather than to one chat.

There is no route that creates a rule directly. A grant only ever comes from
answering a real request, so nothing can be granted that was never asked for.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core import permissions
from core.middleware import require_admin, require_user

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    choice: Annotated[str, Field(min_length=1, max_length=40)]


@router.get("")
async def list_permissions(user: str = Depends(require_user)) -> dict:
    return {"rules": permissions.list_rules(), "audit": permissions.audit()[-50:]}


@router.post("/{request_id}/answer")
async def answer(request_id: str, body: Answer, user: str = Depends(require_user)) -> dict:
    # A request id is server-made and lives only while something is waiting on
    # it, so a stale or invented id answers nothing.
    if not permissions.answer(request_id, body.choice, user):
        raise HTTPException(409, "That request is no longer waiting for an answer.")
    return {"status": "answered"}


@router.delete("/{rule_id}")
async def revoke(rule_id: str, user: str = Depends(require_admin)) -> dict:
    if not permissions.revoke(rule_id):
        raise HTTPException(404, "No such rule")
    return {"status": "revoked"}
