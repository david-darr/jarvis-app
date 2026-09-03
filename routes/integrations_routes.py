"""Settings > Integrations (David's ask 2026-08-31, matching Odysseus's
"Add Integration" panel). Admin-gated: API-service keys and MCP server
config both widen what the agent can reach.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import contacts_store, dav_client, integrations
from core.middleware import require_admin
from services.calendar_service import calendar_service

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class CreateApiServiceRequest(BaseModel):
    name: str
    base_url: str
    api_key: Optional[str] = None


class CreateMcpServerRequest(BaseModel):
    name: str
    mcp_type: str  # "stdio" or "http"
    command: Optional[str] = None
    args: Optional[list[str]] = None
    url: Optional[str] = None
    api_key: Optional[str] = None


@router.get("")
async def list_integrations(user: str = Depends(require_admin)) -> list[dict]:
    return integrations.list_integrations()


@router.post("/api-service")
async def create_api_service(body: CreateApiServiceRequest, user: str = Depends(require_admin)) -> dict:
    return integrations.create_api_service(body.name, body.base_url, body.api_key)


@router.post("/mcp-server")
async def create_mcp_server(body: CreateMcpServerRequest, user: str = Depends(require_admin)) -> dict:
    try:
        return integrations.create_mcp_server(body.name, body.mcp_type, body.command, body.args, body.url, body.api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class CreateDavRequest(BaseModel):
    name: str
    url: str
    username: str
    password: str


@router.post("/caldav")
async def create_caldav(body: CreateDavRequest, user: str = Depends(require_admin)) -> dict:
    """Creates the integration and runs its first sync immediately — so
    "Add" visibly does something real rather than just saving credentials
    that only take effect on some later manual step."""
    item = integrations.create_dav("caldav_calendar", body.name, body.url, body.username, body.password)
    try:
        await _sync_caldav(item["id"])
    except Exception as e:
        integrations.delete_integration(item["id"])
        raise HTTPException(status_code=400, detail=f"couldn't reach that calendar: {str(e)[:300]}")
    return integrations.get_integration_masked(item["id"])


@router.post("/carddav")
async def create_carddav(body: CreateDavRequest, user: str = Depends(require_admin)) -> dict:
    item = integrations.create_dav("carddav_contacts", body.name, body.url, body.username, body.password)
    try:
        await _sync_carddav(item["id"])
    except Exception as e:
        integrations.delete_integration(item["id"])
        raise HTTPException(status_code=400, detail=f"couldn't reach that address book: {str(e)[:300]}")
    return integrations.get_integration_masked(item["id"])


async def _sync_caldav(item_id: str) -> int:
    url, username, password = integrations.get_dav_credentials(item_id)
    events = await dav_client.sync_calendar(url, username, password)
    count = calendar_service.replace_synced_events(item_id, events)
    integrations.record_sync_count(item_id, count)
    return count


async def _sync_carddav(item_id: str) -> int:
    url, username, password = integrations.get_dav_credentials(item_id)
    contacts = await dav_client.sync_contacts(url, username, password)
    count = contacts_store.replace_synced_contacts(item_id, contacts)
    integrations.record_sync_count(item_id, count)
    return count


class CreateIcalFeedRequest(BaseModel):
    name: str
    url: str
    username: Optional[str] = None
    password: Optional[str] = None


@router.post("/ical")
async def create_ical_feed(body: CreateIcalFeedRequest, user: str = Depends(require_admin)) -> dict:
    """Plain iCal (.ics) feed subscription (David's ask 2026-08-31) —
    simpler than CalDAV: one GET, one document, no PROPFIND. Most public
    feeds (Google's "secret address in iCal format", Apple share links)
    need no auth, so username/password are optional here unlike CalDAV."""
    item = integrations.create_ical_feed(body.name, body.url, body.username, body.password)
    try:
        await _sync_ical_feed(item["id"])
    except Exception as e:
        integrations.delete_integration(item["id"])
        raise HTTPException(status_code=400, detail=f"couldn't reach that feed: {str(e)[:300]}")
    return integrations.get_integration_masked(item["id"])


async def _sync_ical_feed(item_id: str) -> int:
    url, username, password = integrations.get_ical_credentials(item_id)
    events = await dav_client.sync_ical_feed(url, username, password)
    count = calendar_service.replace_synced_events(item_id, events)
    integrations.record_sync_count(item_id, count)
    return count


@router.post("/{item_id}/sync")
async def sync_integration(item_id: str, user: str = Depends(require_admin)) -> dict:
    item = integrations.get_integration(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="integration not found")
    try:
        if item["kind"] == "caldav_calendar":
            count = await _sync_caldav(item_id)
        elif item["kind"] == "carddav_contacts":
            count = await _sync_carddav(item_id)
        elif item["kind"] == "ical_feed":
            count = await _sync_ical_feed(item_id)
        else:
            raise HTTPException(status_code=400, detail="this integration type has no sync action")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"sync failed: {str(e)[:300]}")
    return {"ok": True, "count": count}


@router.get("/contacts")
async def list_contacts(user: str = Depends(require_admin)) -> list[dict]:
    return contacts_store.list_contacts()


@router.delete("/{item_id}")
async def delete_integration(item_id: str, user: str = Depends(require_admin)) -> dict:
    item = integrations.get_integration(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="integration not found")
    # Real gap found live: deleting only removed the integration record,
    # leaving its synced calendar events/contacts orphaned forever (no
    # integration left to re-sync or clear them through). Cascade-clean here.
    if item["kind"] in ("caldav_calendar", "ical_feed"):
        calendar_service.replace_synced_events(item_id, [])
    elif item["kind"] == "carddav_contacts":
        contacts_store.replace_synced_contacts(item_id, [])
    integrations.delete_integration(item_id)
    return {"ok": True}
