"""Documents CRUD + search — the Library tab (Phase 7). Deliberately scoped
lighter than Odysseus's document/RAG system — see services/documents_service.py's
module docstring."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.middleware import require_user
from services import documents_service

router = APIRouter(prefix="/api/documents", tags=["documents"])


class CreateDocumentRequest(BaseModel):
    title: str
    content: str = ""
    tags: Optional[list[str]] = None


class UpdateDocumentRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None


class ImportDocumentRequest(BaseModel):
    filename: str
    content: str


@router.get("")
async def list_documents(user: str = Depends(require_user)) -> list[dict]:
    return documents_service.list_documents()


@router.get("/search")
async def search_documents(q: str = Query(...), user: str = Depends(require_user)) -> list[dict]:
    return documents_service.search_documents(q)


@router.get("/{doc_id}")
async def get_document(doc_id: str, user: str = Depends(require_user)) -> dict:
    doc = documents_service.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    return doc


@router.post("")
async def create_document(body: CreateDocumentRequest, user: str = Depends(require_user)) -> dict:
    return documents_service.create_document(body.title, body.content, body.tags)


@router.post("/import")
async def import_document(body: ImportDocumentRequest, user: str = Depends(require_user)) -> dict:
    return documents_service.import_document(body.filename, body.content)


@router.patch("/{doc_id}")
async def update_document(doc_id: str, body: UpdateDocumentRequest, user: str = Depends(require_user)) -> dict:
    try:
        return documents_service.update_document(doc_id, body.title, body.content, body.tags)
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")


@router.delete("/{doc_id}")
async def delete_document(doc_id: str, user: str = Depends(require_user)) -> dict:
    documents_service.delete_document(doc_id)
    return {"ok": True}
