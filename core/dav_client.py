"""Minimal CalDAV/CardDAV client — David's ask 2026-08-31 (add Calendar and
Contacts to Settings > Integrations, after initially scoping them out for
lack of any WebDAV support). No new dependency: uses httpx (already a
requirement) for the WebDAV requests and a small hand-rolled RFC 5545/6350
parser rather than pulling in `caldav`/`vobject`/`icalendar`.

Scope, stated plainly rather than silently: this does PROPFIND (Depth: 1) to
list resource hrefs in a calendar/address-book collection, then GETs and
parses each one. That's real WebDAV, not a fake stub — but it's simpler than
a full calendar-query REPORT with server-side date filtering, so a very
large remote collection fetches everything rather than a filtered range.
Read-only, one-way sync (remote -> JARVIS) — no writeback, matching the
"deliberately out of this pass" scoping already stated for two-way CalDAV in
services/calendar_service.py.
"""
import re
from typing import Optional

import httpx

TIMEOUT_SECONDS = 30

_PROPFIND_BODY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:resourcetype/><d:getcontenttype/></d:prop>
</d:propfind>"""

_HREF_RE = re.compile(r"<[a-zA-Z0-9]*:?href>([^<]+)</[a-zA-Z0-9]*:?href>", re.IGNORECASE)


async def list_hrefs(url: str, username: str, password: str, suffix: str) -> list[str]:
    """PROPFIND Depth:1 on a collection URL, return hrefs ending in `suffix`
    (.ics or .vcf) — the individual resources to fetch and parse."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, auth=(username, password)) as client:
        resp = await client.request(
            "PROPFIND", url,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content=_PROPFIND_BODY,
        )
        resp.raise_for_status()
        hrefs = _HREF_RE.findall(resp.text)
        return [h for h in hrefs if h.lower().endswith(suffix)]


async def fetch_resource(base_url: str, href: str, username: str, password: str) -> str:
    from urllib.parse import urljoin
    full_url = urljoin(base_url, href)
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, auth=(username, password)) as client:
        resp = await client.get(full_url)
        resp.raise_for_status()
        return resp.text


def _unfold(text: str) -> str:
    """RFC 5545/6350 line folding: a continuation line starts with a space or
    tab and should be joined to the previous line."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _extract_field(block: str, name: str) -> Optional[str]:
    # Matches "NAME:value" or "NAME;PARAM=x:value" at line start.
    m = re.search(rf"^{name}(?:;[^:\n]*)?:(.+)$", block, re.MULTILINE)
    return m.group(1).strip() if m else None


def parse_vevents(ics_text: str) -> list[dict]:
    """Minimal VEVENT extraction: UID, SUMMARY, DTSTART, DTEND, all-day
    detection (DATE-only values have no 'T'). Recurrence rules (RRULE) are
    not expanded — each VEVENT block becomes one event as literally written,
    matching this module's stated "simpler than a full CalDAV client" scope."""
    text = _unfold(ics_text)
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL):
        uid = _extract_field(block, "UID")
        summary = _extract_field(block, "SUMMARY")
        dtstart = _extract_field(block, "DTSTART")
        dtend = _extract_field(block, "DTEND")
        if not (uid and summary and dtstart):
            continue
        all_day = "T" not in dtstart
        events.append({
            "uid": uid,
            "title": summary,
            "start": _to_iso(dtstart),
            "end": _to_iso(dtend or dtstart),
            "all_day": all_day,
        })
    return events


def parse_vcards(vcf_text: str) -> list[dict]:
    """Minimal vCard extraction: UID, FN (full name), EMAIL, TEL."""
    text = _unfold(vcf_text)
    contacts = []
    for block in re.findall(r"BEGIN:VCARD(.*?)END:VCARD", text, re.DOTALL):
        uid = _extract_field(block, "UID")
        fn = _extract_field(block, "FN")
        email = _extract_field(block, "EMAIL")
        tel = _extract_field(block, "TEL")
        if not fn:
            continue
        contacts.append({"uid": uid or fn, "name": fn, "email": email, "phone": tel})
    return contacts


def _to_iso(dav_dt: str) -> str:
    """DAV date(time) values (20260901T140000Z or 20260901) -> ISO 8601."""
    dav_dt = dav_dt.strip()
    if "T" in dav_dt:
        date_part, time_part = dav_dt.split("T", 1)
        z = time_part.endswith("Z")
        time_part = time_part.rstrip("Z")
        iso = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
        return iso + ("Z" if z else "")
    return f"{dav_dt[0:4]}-{dav_dt[4:6]}-{dav_dt[6:8]}"


async def sync_calendar(url: str, username: str, password: str) -> list[dict]:
    hrefs = await list_hrefs(url, username, password, ".ics")
    events = []
    for href in hrefs:
        try:
            text = await fetch_resource(url, href, username, password)
            events.extend(parse_vevents(text))
        except httpx.HTTPError:
            continue
    return events


async def sync_contacts(url: str, username: str, password: str) -> list[dict]:
    hrefs = await list_hrefs(url, username, password, ".vcf")
    contacts = []
    for href in hrefs:
        try:
            text = await fetch_resource(url, href, username, password)
            contacts.extend(parse_vcards(text))
        except httpx.HTTPError:
            continue
    return contacts


async def sync_ical_feed(url: str, username: str | None = None, password: str | None = None) -> list[dict]:
    """Plain iCal (.ics) feed subscription — David's ask 2026-08-31. Simpler
    than CalDAV: one document containing every VEVENT (a Google Calendar
    "secret address in iCal format", an Apple webcal:// share link, etc.),
    fetched with a single GET rather than PROPFIND-then-fetch-each-resource.
    `webcal://` is just a hint for calendar apps to treat the URL as a
    subscription — the actual transport is plain HTTP(S), so it's normalized
    to `https://` here. Most public iCal feeds need no auth at all;
    username/password are optional for the ones that do (HTTP basic auth)."""
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    auth = (username, password) if username and password else None
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, auth=auth, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return parse_vevents(resp.text)
