"""Settings > Integrations (David's ask 2026-08-31, matching Odysseus's
"Add Integration" panel). Admin-gated: API-service keys and MCP server
config both widen what the agent can reach.
"""
import html
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from core import contacts_store, dav_client, integrations, mcp_client, mcp_oauth, sync_engine
from core.middleware import local_api_base, require_admin
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
    auth: Optional[str] = None  # "oauth": used once signed in


@router.get("")
async def list_integrations(user: str = Depends(require_admin)) -> list[dict]:
    return integrations.list_integrations()


@router.post("/api-service")
async def create_api_service(body: CreateApiServiceRequest, user: str = Depends(require_admin)) -> dict:
    return integrations.create_api_service(body.name, body.base_url, body.api_key)


@router.get("/catalog")
async def mcp_catalog(user: str = Depends(require_admin)) -> list[dict]:
    """Known MCP servers to add (core/mcp_catalog.json)."""
    return integrations.mcp_catalog()


@router.post("/mcp-server")
async def create_mcp_server(body: CreateMcpServerRequest, user: str = Depends(require_admin)) -> dict:
    try:
        item = integrations.create_mcp_server(body.name, body.mcp_type, body.command, body.args, body.url,
                                              body.api_key, body.auth)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Checked at once (roadmap phase 6): the answer says whether it works,
    # and its tools are pinned as they are now.
    return await mcp_client.check(item["id"]) or item


class AcceptToolsRequest(BaseModel):
    names: list[str]


@router.post("/{item_id}/check")
async def check_mcp_server(item_id: str, user: str = Depends(require_admin)) -> dict:
    """Check an MCP server now: working, down or signed out, and which of its
    tools are new or changed since they were pinned (core/integrations.py)."""
    checked = await mcp_client.check(item_id)
    if checked is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return checked


@router.post("/{item_id}/tools/accept")
async def accept_mcp_tools(item_id: str, body: AcceptToolsRequest, user: str = Depends(require_admin)) -> dict:
    """Let models use these held tools, as they were when reviewed."""
    try:
        return integrations.accept_tools(item_id, body.names)
    except KeyError:
        raise HTTPException(status_code=404, detail="MCP server not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


# OAuth sign-in for MCP servers (core/mcp_oauth.py). Starting, checking and
# signing out are admin actions; the callback is not, because the browser the
# provider sends back carries no app cookie. It is admitted by the `state` of
# a sign-in an admin started, and anything else is turned away.
@router.post("/{item_id}/oauth/start")
async def oauth_start(item_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return await mcp_oauth.start_sign_in(item_id, local_api_base() + mcp_oauth.CALLBACK_PATH)
    except KeyError:
        raise HTTPException(status_code=404, detail="MCP server not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{item_id}/oauth")
async def oauth_status(item_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        return mcp_oauth.status(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="MCP server not found")


@router.delete("/{item_id}/oauth")
async def oauth_sign_out(item_id: str, user: str = Depends(require_admin)) -> dict:
    try:
        mcp_oauth.sign_out(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"ok": True}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(state: str = "", code: Optional[str] = None, iss: Optional[str] = None,
                         error: Optional[str] = None) -> HTMLResponse:
    ok, message = await mcp_oauth.finish_sign_in(state, code, iss, error)
    title = "Signed in" if ok else "Sign-in did not finish"
    page = (f"<!doctype html><meta charset=utf-8><title>Kairos - {title}</title>"
            f"<body style=\"font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem\">"
            f"<h1 style=\"font-size:1.3rem\">{title}</h1><p>{html.escape(message)}</p></body>")
    return HTMLResponse(page, status_code=200 if ok else 400)


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


# Thin aliases: the real implementations moved to core/sync_engine so the
# daily task and these first-sync-on-create paths share one code path.
async def _sync_caldav(item_id: str) -> int:
    return await sync_engine.sync_integration(item_id)


async def _sync_carddav(item_id: str) -> int:
    return await sync_engine.sync_integration(item_id)


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
    return await sync_engine.sync_integration(item_id)


@router.post("/{item_id}/sync")
async def sync_integration(item_id: str, user: str = Depends(require_admin)) -> dict:
    # Delegates to core/sync_engine so this button and the nightly Sync All
    # task run identical code. They used to be able to drift apart, which is
    # the kind of difference nobody notices until the unattended path breaks.
    try:
        count = await sync_engine.sync_integration(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="integration not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"sync failed: {str(e)[:300]}")
    return {"ok": True, "count": count}


@router.post("/sync-all")
async def sync_all_now(user: str = Depends(require_admin)) -> dict:
    """Manual trigger for the same pass the daily task runs (David's ask
    2026-09-06) — useful right after adding a feed, and for confirming the
    nightly job would work without waiting until morning."""
    results = await sync_engine.sync_all()
    return {"results": results, "summary": sync_engine.format_results(results)}


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
