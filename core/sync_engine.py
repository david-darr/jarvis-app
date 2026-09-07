"""One place that knows how to refresh every external data source.

David's ask 2026-09-06: calendars, imported calendar feeds, Canvas, and any
API a user adds later should all refresh themselves daily, instead of the user
having to press Sync on each one. Before this, syncing existed only as route
handlers in routes/integrations_routes.py and routes/tab_school.py — reachable
by a button click and nothing else, so an unattended app slowly went stale.

Two kinds of source, deliberately:

- **Integrations** (CalDAV, CardDAV, iCal feeds) are enumerated at run time
  from integrations.json, so a feed added tomorrow is picked up with no code
  change and no registration call.
- **Registered providers** are for anything that isn't an integration record —
  today the School tab's Canvas sync. A tab registers itself when its routes
  module is imported, which is exactly when that tab is actually mounted, so
  a disabled tab contributes nothing. This is the extension point for the
  "other added apis by users" half of the ask: a user-authored tab module
  calls register_provider() at import and joins the daily cycle for free.

Every sync here is best-effort and isolated: one unreachable feed must never
stop the rest, because they all share a single nightly run.
"""
import logging
from typing import Awaitable, Callable, Optional

from core import integrations

logger = logging.getLogger(__name__)

# name -> coroutine returning anything; its repr is only used for the summary.
_providers: dict[str, Callable[[], Awaitable]] = {}


def register_provider(name: str, fn: Callable[[], Awaitable]) -> None:
    """Add a non-integration source to the daily cycle. Idempotent by name, so
    a module re-imported under a second package path (routes.tab_x and
    tab_x both resolve for user tabs — see core/custom_tabs.py) registers once
    rather than syncing twice."""
    _providers[name] = fn


def list_providers() -> list[str]:
    return sorted(_providers)


async def sync_integration(item_id: str) -> int:
    """Refresh one integration by id, returning how many records it produced.

    Lives here rather than in the route module so the daily task and the
    Sync button run the exact same code — a nightly path that drifts from the
    interactive one is a bug generator.
    """
    from core import contacts_store, dav_client
    from services.calendar_service import calendar_service

    item = integrations.get_integration(item_id)
    if item is None:
        raise KeyError(f"no such integration: {item_id}")

    kind = item["kind"]
    if kind == "caldav_calendar":
        url, username, password = integrations.get_dav_credentials(item_id)
        events = await dav_client.sync_calendar(url, username, password)
        count = calendar_service.replace_synced_events(item_id, events)
    elif kind == "carddav_contacts":
        url, username, password = integrations.get_dav_credentials(item_id)
        contacts = await dav_client.sync_contacts(url, username, password)
        count = contacts_store.replace_synced_contacts(item_id, contacts)
    elif kind == "ical_feed":
        url, username, password = integrations.get_ical_credentials(item_id)
        events = await dav_client.sync_ical_feed(url, username, password)
        count = calendar_service.replace_synced_events(item_id, events)
    else:
        raise ValueError("this integration type has no sync action")

    integrations.record_sync_count(item_id, count)
    return count


SYNCABLE_KINDS = ("caldav_calendar", "carddav_contacts", "ical_feed")


async def sync_all() -> list[dict]:
    """Refresh everything. Returns one result dict per source:
    {"name", "ok", "detail"} — never raises, so the caller can report a mixed
    outcome rather than losing the successes to one failure.
    """
    results: list[dict] = []

    for item in integrations.list_integrations():
        if item["kind"] not in SYNCABLE_KINDS:
            continue  # api_service / mcp_server have nothing to pull on a timer
        try:
            count = await sync_integration(item["id"])
            results.append({"name": item["name"], "ok": True, "detail": f"{count} items"})
        except Exception as e:
            logger.warning("sync_all: integration %r failed: %s", item["name"], e)
            results.append({"name": item["name"], "ok": False, "detail": str(e)[:200]})

    for name, fn in sorted(_providers.items()):
        try:
            out = await fn()
            detail = ""
            if isinstance(out, dict):
                if "count" in out:
                    detail = f"{out['count']} items"
                    if out.get("source"):
                        detail += f" via {out['source']}"
                else:
                    detail = ", ".join(f"{k}={v}" for k, v in list(out.items())[:3])
            elif out is not None:
                detail = str(out)[:200]
            results.append({"name": name, "ok": True, "detail": detail})
        except Exception as e:
            logger.warning("sync_all: provider %r failed: %s", name, e)
            results.append({"name": name, "ok": False, "detail": str(e)[:200]})

    return results


def format_results(results: list[dict]) -> str:
    if not results:
        return "Nothing to sync — no calendar feeds or syncing integrations are connected."
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    lines = [f"Synced {len(ok)} of {len(results)} source(s)."]
    for r in ok:
        lines.append(f"- {r['name']}: {r['detail']}" if r["detail"] else f"- {r['name']}: ok")
    for r in bad:
        lines.append(f"- {r['name']}: FAILED — {r['detail']}")
    return "\n".join(lines)


def last_sync_summary() -> Optional[dict]:
    """Most recent per-integration counts, for the Integrations UI."""
    return {i["name"]: i.get("last_synced_count") for i in integrations.list_integrations()}
