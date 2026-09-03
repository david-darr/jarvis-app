"""Email account CRUD + connection test + inbox list + send — the Email tab."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.middleware import require_user
from services.email_service import email_service

router = APIRouter(prefix="/api/email", tags=["email"])


class CreateAccountRequest(BaseModel):
    email: str
    password: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str
    smtp_port: int = 465
    smtp_security: str = "ssl"


class SendRequest(BaseModel):
    to: str
    subject: str
    body: str


@router.get("/accounts")
async def list_accounts(user: str = Depends(require_user)) -> list[dict]:
    return email_service.list_accounts()


@router.post("/accounts")
async def create_account(body: CreateAccountRequest, user: str = Depends(require_user)) -> dict:
    return email_service.create_account(
        body.email, body.password, body.imap_host, body.imap_port,
        body.smtp_host, body.smtp_port, body.smtp_security,
    )


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, user: str = Depends(require_user)) -> dict:
    email_service.delete_account(account_id)
    return {"ok": True}


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: str, user: str = Depends(require_user)) -> dict:
    try:
        return email_service.test_connection(account_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found")


@router.get("/accounts/{account_id}/messages")
async def list_messages(account_id: str, folder: str = "INBOX", limit: int = 20, user: str = Depends(require_user)) -> list[dict]:
    try:
        return email_service.list_messages(account_id, folder, limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IMAP error: {e}")


@router.post("/accounts/{account_id}/send")
async def send_message(account_id: str, body: SendRequest, user: str = Depends(require_user)) -> dict:
    try:
        email_service.send_message(account_id, body.to, body.subject, body.body)
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SMTP error: {e}")
    return {"ok": True}
