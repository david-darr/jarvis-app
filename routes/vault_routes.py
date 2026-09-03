"""Vault graph + note read/write — the Brain tab's Vault view (David's ask
2026-09-01, ported from the original kiosk's /vault-graph and /vault-note)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core import vault_graph, vault_sync
from core.middleware import require_user

router = APIRouter(prefix="/api/vault", tags=["vault"])


@router.post("/sync")
async def sync_vault(user: str = Depends(require_user)) -> dict:
    """Pull the vault's task list into Notes (David's ask 2026-09-03 — the
    app answered "no priorities" while the vault was full of them). Runs
    automatically at startup; this is the manual re-run for when the vault
    has been edited in Obsidian while the app is open."""
    return vault_sync.sync_from_vault()


class WriteNoteRequest(BaseModel):
    path: str
    content: str


@router.get("/graph")
async def get_vault_graph(user: str = Depends(require_user)) -> dict:
    return vault_graph.build_vault_graph()


@router.get("/note")
async def get_vault_note(path: str = Query(...), user: str = Depends(require_user)) -> dict:
    content = vault_graph.read_vault_note(path)
    if content is None:
        raise HTTPException(status_code=404, detail="note not found")
    return {"path": path, "content": content}


@router.post("/note")
async def post_vault_note(body: WriteNoteRequest, user: str = Depends(require_user)) -> dict:
    ok = vault_graph.write_vault_note(body.path, body.content)
    if not ok:
        raise HTTPException(status_code=404, detail="note not found")
    return {"ok": True}
