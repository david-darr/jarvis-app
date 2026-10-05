"""Settings > Channels > Connectors (core/connectors). Managing connectors
is admin-only: a connector can let messages reach JARVIS and its agents.
The webhook endpoint is public by necessity and trusts nothing until the
platform's own signature has been verified (core/connectors/platforms)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core import model_endpoints
from core.connectors import KINDS, hub, store
from core.middleware import require_admin

router = APIRouter(prefix="/api/connectors", tags=["connectors"])


class ConnectorBody(BaseModel):
    kind: Optional[str] = None
    name: Optional[str] = None
    values: Optional[dict] = None
    allowed_senders: Optional[list[str]] = None
    open: Optional[bool] = None
    model_endpoint_id: Optional[str] = None
    enabled: Optional[bool] = None


class TestBody(BaseModel):
    text: str = "Test message from JARVIS."


def _kind(kind: str):
    cls = KINDS.get(kind)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"unknown platform: {kind}")
    return cls


def _shown(record: dict) -> dict:
    cls = KINDS.get(record["kind"])
    status = hub.status.get(record["id"]) or {"state": "off" if not record.get("enabled") else "starting"}
    return {**store.public(record, cls.fields if cls else ()), "label": cls.label if cls else record["kind"],
            "two_way": bool(cls and cls.two_way), "webhook": bool(cls and cls.webhook), "status": status,
            "webhook_path": f"/api/connectors/{record['id']}/webhook" if cls and cls.webhook else None}


@router.get("/kinds")
async def kinds(user: str = Depends(require_admin)) -> list[dict]:
    return [{"kind": c.kind, "label": c.label, "description": c.description, "docs_url": c.docs_url,
             "two_way": c.two_way, "webhook": c.webhook, "sender_help": c.sender_help,
             "fields": [f.describe() for f in c.fields]} for c in KINDS.values()]


@router.get("")
async def list_connectors(user: str = Depends(require_admin)) -> list[dict]:
    return [_shown(r) for r in store.list_records()]


@router.post("")
async def create_connector(body: ConnectorBody, user: str = Depends(require_admin)) -> dict:
    cls = _kind(body.kind or "")
    if body.model_endpoint_id and model_endpoints.get_endpoint(body.model_endpoint_id) is None:
        raise HTTPException(status_code=400, detail="pick a model added in Settings")
    try:
        record = store.create(cls.kind, body.name or cls.label, body.values or {}, cls.fields,
                              body.allowed_senders or [], bool(body.open), body.model_endpoint_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await hub.start(record["id"])
    return _shown(record)


@router.patch("/{connector_id}")
async def update_connector(connector_id: str, body: ConnectorBody, user: str = Depends(require_admin)) -> dict:
    record = store.get_record(connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="connector not found")
    cls = _kind(record["kind"])
    fields = body.model_dump(exclude_unset=True)
    if fields.get("model_endpoint_id") and model_endpoints.get_endpoint(fields["model_endpoint_id"]) is None:
        raise HTTPException(status_code=400, detail="pick a model added in Settings")
    try:
        record = store.update(connector_id, cls.fields, name=fields.get("name"), values=fields.get("values"),
                              allowed_senders=fields.get("allowed_senders"), open_to_anyone=fields.get("open"),
                              enabled=fields.get("enabled"),
                              **({"model_endpoint_id": fields["model_endpoint_id"]} if "model_endpoint_id" in fields else {}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await hub.start(connector_id)  # picks up the new settings, or stops it when disabled
    return _shown(record)


@router.delete("/{connector_id}")
async def delete_connector(connector_id: str, user: str = Depends(require_admin)) -> dict:
    await hub.stop(connector_id)
    store.delete(connector_id)
    hub.status.pop(connector_id, None)
    return {"ok": True}


@router.post("/{connector_id}/test")
async def test_connector(connector_id: str, body: TestBody, user: str = Depends(require_admin)) -> dict:
    record = store.get_record(connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="connector not found")
    adapter = hub.adapters.get(connector_id) or hub.build(record)
    if not adapter.default_target:
        raise HTTPException(status_code=400, detail="set where notifications go first")
    try:
        await hub.send_reply(adapter, adapter.default_target, body.text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"sending failed: {e}")
    return {"ok": True}


@router.api_route("/{connector_id}/webhook", methods=["GET", "POST"], include_in_schema=False)
async def webhook(connector_id: str, request: Request) -> Response:
    """Public: called by the platform. The adapter verifies its signature
    (or, for WhatsApp's setup check, its verify token) before anything."""
    body = await request.body()
    if len(body) > 2_000_000:
        return Response(status_code=413)
    status, content, media_type = await hub.webhook(connector_id, request.method, dict(request.headers),
                                                    dict(request.query_params), body)
    return Response(content=content, status_code=status, media_type=media_type)
