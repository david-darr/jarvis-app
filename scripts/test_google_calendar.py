"""Drive previews and live Google Calendar, with Google's HTTP layer mocked.

No server, subprocess or Google account is used. Run with the project Python.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import shutil
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

_TEST_PARENT = Path(__file__).resolve().parents[1] / "data"
_TEST_PARENT.mkdir(exist_ok=True)
_DATA = _TEST_PARENT / ("google-test-" + uuid.uuid4().hex)
_DATA.mkdir()
os.environ["JARVIS_DATA_DIR"] = str(_DATA)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI, HTTPException
from core import google_workspace as google, google_chat_tools, tool_registry
from core.google_preview import sanitize_html
from core.middleware import require_admin, require_user, SecurityHeadersMiddleware
from core.turn_taint import TurnTaint
from routes import google_routes, calendar_routes

REAL_CLIENT = httpx.AsyncClient
START, END = "2026-10-01T00:00:00-04:00", "2026-11-01T00:00:00-04:00"
CALENDAR = {"id": "david@example.com", "summary": "Personal", "primary": True,
            "accessRole": "owner", "backgroundColor": "#123456", "timeZone": "America/New_York"}
TIMED = {"id": "timed", "summary": "Check-in", "start": {"dateTime": "2026-10-08T09:00:00-04:00"},
         "end": {"dateTime": "2026-10-08T10:00:00-04:00"}}
ALL_DAY = {"id": "all_day", "summary": "Trip", "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-10"}}
EVENT = {"summary": "New event", "start": {"dateTime": "2026-10-08T09:00:00", "timeZone": "America/New_York"},
         "end": {"dateTime": "2026-10-08T10:00:00", "timeZone": "America/New_York"}}
PNG = b"\x89PNG\r\n\x1a\nfixture"


class GoogleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {"client_id": "fixture.apps.googleusercontent.com", "refresh_token": "saved",
                     "granted_scopes": list(google.SCOPES)}
        self.requests = []
        self.calendars = [deepcopy(CALENDAR)]
        self.events = [deepcopy(TIMED), deepcopy(ALL_DAY), {"id": "cancelled", "status": "cancelled"}]
        self.info = {"id": "file", "name": "Brief", "mimeType": "application/vnd.google-apps.document",
                     "thumbnailLink": "https://lh3.googleusercontent.com/fixture=s200"}
        self.content = b'<h1>Brief</h1><script>evil()</script><p onerror="evil()">Safe</p><img src="https://evil"><a href="javascript:evil()">Link</a>'
        self.response_override = None
        def save(data):
            self.data = deepcopy(data)
        self.patches = [patch.object(google, "_load", lambda: deepcopy(self.data)),
                        patch.object(google, "_save", save), patch.object(google, "_token", AsyncMock(return_value="access")),
                        patch.object(google.httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(self.http), **kwargs))]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)
        google.invalidate_calendar()

    def http(self, req):
        self.requests.append(req)
        if self.response_override:
            override = self.response_override(req)
            if override is not None:
                return override
        path = req.url.path
        if req.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"refresh_token": "refresh", "access_token": "access", "scope": " ".join(google.SCOPES)})
        if req.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": "david@example.com"})
        if req.url.host == "lh3.googleusercontent.com":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        if path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": self.calendars})
        if "/calendar/v3/calendars/" in path:
            if req.method == "GET":
                return httpx.Response(200, json={"items": self.events})
            if req.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"id": "created", **(json.loads(req.content) if req.content else {})})
        if path.endswith("/about"):
            return httpx.Response(200, json={"storageQuota": {"usage": "1024", "limit": "2048"}})
        if path.endswith("/export") or req.url.params.get("alt") == "media":
            return httpx.Response(200, content=self.content, headers={"content-type": req.url.params.get("mimeType") or self.info["mimeType"]})
        if path.endswith("/files"):
            return httpx.Response(200, json={"files": [self.info]})
        if path.endswith("/files/file"):
            return httpx.Response(200, json=self.info)
        raise AssertionError(f"Unexpected Google HTTP request: {req.method} {req.url}")

    async def web(self, path, method="GET", body=None, admin=True):
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)
        app.include_router(google_routes.router)
        app.include_router(calendar_routes.router)
        def identity():
            if not admin:
                raise HTTPException(403, "admin privileges required")
            return "fixture"
        app.dependency_overrides[require_admin] = identity
        app.dependency_overrides[require_user] = lambda: "fixture"
        async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, json=body)

    async def test_sign_in_records_granted_scopes_and_requests_consent(self):
        from urllib.parse import parse_qs, urlsplit
        url = google.start_sign_in("http://127.0.0.1:8420/api/google/oauth/callback")
        args = parse_qs(urlsplit(url).query)
        self.assertEqual(args["prompt"], ["consent"])
        self.assertIn(google.CALENDAR_SCOPE, args["scope"][0].split())
        await google.finish_sign_in(args["state"][0], "code", "http://127.0.0.1:8420/api/google/oauth/callback")
        self.assertIn(google.CALENDAR_SCOPE, self.data["granted_scopes"])
        result = await self.web("/api/google/status")
        self.assertTrue(result.json()["calendar_connected"])
        self.assertNotIn("refresh_token", result.json())

    async def test_missing_scope_and_legacy_account_offer_reconnect(self):
        for scopes in ([], ["https://www.googleapis.com/auth/drive"]):
            self.data["granted_scopes"] = scopes
            self.assertTrue(google.status()["connected"])
            self.assertFalse(google.status()["calendar_connected"])
            response = await self.web("/api/google/calendar/calendars")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "Reconnect to add Calendar")
        self.assertEqual(self.requests, [])

    async def test_calendar_mapping_keeps_all_day_dates_exclusive_and_offsets(self):
        events = await google.calendar_events([CALENDAR["id"]], START, END)
        timed = next(e for e in events if not e["all_day"])
        all_day = next(e for e in events if e["all_day"])
        self.assertEqual((all_day["start"], all_day["end"]), ("2026-10-08", "2026-10-10"))
        self.assertEqual(timed["start"], "2026-10-08T09:00:00-04:00")
        self.assertEqual((timed["source"], timed["calendar_color"], timed["calendar_id"]), ("google", "#123456", CALENDAR["id"]))
        self.assertTrue(timed["editable"])
        self.assertEqual(len(events), 2)
        request = self.requests[-1]
        self.assertEqual(request.url.params["singleEvents"], "true")
        self.assertEqual(request.url.params["timeMin"], START)

    async def test_zone_without_offset_is_resolved_including_dst(self):
        self.assertEqual(google._timed("2026-10-08T09:00:00", "America/New_York"), "2026-10-08T09:00:00-04:00")
        self.assertEqual(google._timed("2026-12-08T09:00:00", "America/New_York"), "2026-12-08T09:00:00-05:00")

    async def test_events_from_different_offsets_sort_by_their_actual_time(self):
        self.events = [{"id": "east", "start": {"dateTime": "2026-10-08T09:00:00+09:00"}, "end": {"dateTime": "2026-10-08T10:00:00+09:00"}},
                       {"id": "west", "start": {"dateTime": "2026-10-08T02:00:00-04:00"}, "end": {"dateTime": "2026-10-08T03:00:00-04:00"}}]
        self.assertEqual([item["id"] for item in await google.calendar_events([CALENDAR["id"]], START, END)], ["east", "west"])

    async def test_event_cache_expires_at_sixty_seconds_and_is_not_mutable(self):
        with patch.object(google.time, "monotonic", return_value=100):
            first = await google.calendar_events([CALENDAR["id"]], START, END)
            first[0]["title"] = "Changed locally"
        with patch.object(google.time, "monotonic", return_value=159):
            self.assertNotEqual((await google.calendar_events([CALENDAR["id"]], START, END))[0]["title"], "Changed locally")
        self.assertEqual(len(self.requests), 2)
        with patch.object(google.time, "monotonic", return_value=160):
            await google.calendar_events([CALENDAR["id"]], START, END)
        self.assertEqual(len(self.requests), 4)

    async def test_mutations_reach_google_and_invalidate_ranges(self):
        for action in ("create", "update", "delete", "quick_add"):
            await google.calendar_events([CALENDAR["id"]], START, END)
            response = await self.web("/api/google/calendar/action", "POST", {
                "action": action, "calendar_id": CALENDAR["id"], "event_id": "timed", "event": EVENT, "text": "Lunch tomorrow"})
            self.assertEqual(response.status_code, 200, response.text)
            req = self.requests[-1]
            self.assertEqual(req.method, {"create": "POST", "update": "PATCH", "delete": "DELETE", "quick_add": "POST"}[action])
            if action in ("create", "update"):
                self.assertEqual(json.loads(req.content)["start"]["dateTime"], "2026-10-08T09:00:00-04:00")
            if action == "quick_add":
                self.assertEqual(req.url.params["text"], "Lunch tomorrow")
            self.assertFalse(google._calendar_cache)

    async def test_a_range_started_before_invalidation_cannot_refill_the_cache(self):
        def invalidate_during_read(req):
            if req.url.path.endswith("/events"):
                google.invalidate_calendar()
        self.response_override = invalidate_during_read
        self.assertEqual(len(await google.calendar_events([CALENDAR["id"]], START, END)), 2)
        self.assertFalse(google._calendar_cache)

    async def test_disconnecting_clears_scope_visibility_and_range_cache(self):
        google.calendar_settings([CALENDAR["id"]])
        await google.calendar_events([CALENDAR["id"]], START, END)
        self.assertTrue(google._calendar_cache)
        status = google.disconnect()
        self.assertFalse(status["connected"])
        self.assertFalse(status["calendar_connected"])
        self.assertEqual(status["granted_scopes"], [])
        self.assertIsNone(status["calendar_ids"])
        self.assertFalse(google._calendar_cache)

    async def test_all_day_write_and_invalid_boundaries(self):
        await google.calendar_action("create", event={"summary": "Day", "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-09"}})
        self.assertEqual(json.loads(self.requests[-1].content)["start"], {"date": "2026-10-08"})
        for event in ({"summary": "Bad", "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-08"}},
                      {"summary": "Bad", "start": {"date": "2026-10-08"}, "end": {"dateTime": END}},
                      {"summary": "Bad", "start": {"date": "2026-02-30"}, "end": {"date": "2026-03-01"}}):
            with self.assertRaises(google.GoogleError):
                await google.calendar_action("create", event=event)

    async def test_calendar_visibility_saved_with_google_and_empty_means_none(self):
        result = await self.web("/api/google/calendar/settings", "PATCH", {"calendar_ids": []})
        self.assertEqual(result.json(), {"calendar_ids": []})
        self.assertEqual(await google.selected_calendar_events(START, END), [])
        self.assertFalse(self.requests)
        google.calendar_settings([CALENDAR["id"]])
        self.assertEqual(len(await google.selected_calendar_events(START, END)), 2)

    async def test_pagination_for_calendars_and_events(self):
        def pages(req):
            if req.url.path.endswith("calendarList") and not req.url.params.get("pageToken"):
                return httpx.Response(200, json={"items": [], "nextPageToken": "next"})
            if req.url.path.endswith("events") and not req.url.params.get("pageToken"):
                return httpx.Response(200, json={"items": [], "nextPageToken": "next"})
        self.response_override = pages
        self.assertEqual(len(await google.calendar_events([CALENDAR["id"]], START, END)), 2)
        self.assertEqual(len(self.requests), 4)

    async def test_range_merges_local_notes_feeds_and_google_only_for_admin(self):
        local = [{"id": "local", "source": "calendar"}, {"id": "note", "source": "note"}, {"id": "feed", "source": "calendar"}]
        with patch.object(calendar_routes.calendar_service, "list_range", return_value=local), \
             patch("core.tab_hooks.calendar_items", AsyncMock(return_value=[{"id": "tab", "source": "tab"}])), \
             patch.object(calendar_routes.auth_manager, "is_admin", return_value=True):
            merged = await calendar_routes.list_events(START, END, "fixture")
            self.assertEqual([e["source"] for e in merged].count("google"), 2)
            self.assertTrue({"local", "note", "feed", "tab"}.issubset({e["id"] for e in merged}))
        self.requests.clear()
        with patch.object(calendar_routes.calendar_service, "list_range", return_value=[]), \
             patch("core.tab_hooks.calendar_items", AsyncMock(return_value=[])), \
             patch.object(calendar_routes.auth_manager, "is_admin", return_value=False):
            self.assertEqual(await calendar_routes.list_events(START, END, "plain"), [])
        self.assertFalse(self.requests)

    async def test_google_outage_keeps_local_calendar_working(self):
        def outage(req):
            return httpx.Response(503, json={"error": {"message": "Unavailable"}})
        self.response_override = outage
        with patch.object(calendar_routes.calendar_service, "list_range", return_value=[{"id": "local"}]), \
             patch("core.tab_hooks.calendar_items", AsyncMock(return_value=[])), \
             patch.object(calendar_routes.auth_manager, "is_admin", return_value=True), \
             self.assertLogs("routes.calendar_routes", level="WARNING"):
            self.assertEqual(await calendar_routes.list_events(START, END, "fixture"), [{"id": "local"}])

    async def test_every_calendar_chat_mutation_asks_permission_and_denial_stops_http(self):
        ctx = tool_registry.ToolContext(session_id="session", is_admin=True, turn_taint=TurnTaint())
        ctx.turn_taint.mark("Outside content")
        for action in ("create", "update", "delete", "quick_add"):
            args = {"action": action, "calendar_id": CALENDAR["id"], "event_id": "timed", "event": EVENT, "text": "Lunch"}
            with patch.object(google_chat_tools.permissions, "decide", AsyncMock(return_value=SimpleNamespace(behavior="allow"))) as decide:
                await google_chat_tools.execute("calendar", args, ctx)
                self.assertEqual(decide.await_count, 1)
                self.assertTrue(decide.call_args.kwargs["force_prompt"])
                self.assertEqual(decide.call_args.kwargs["tool"], f"google_calendar_{action}")
            self.requests.clear()
            with patch.object(google_chat_tools.permissions, "decide", AsyncMock(return_value=SimpleNamespace(behavior="deny", reason="Denied"))):
                self.assertIn("Not run", await google_chat_tools.execute("calendar", args, ctx))
            self.assertFalse(self.requests)

    async def test_calendar_chat_reads_taint_and_are_registered_for_all_admin_surfaces(self):
        for action in ("list_calendars", "list_events"):
            ctx = tool_registry.ToolContext(is_admin=True, turn_taint=TurnTaint())
            with patch.object(google_chat_tools.permissions, "decide", AsyncMock()) as decide:
                await google_chat_tools.execute("calendar", {"action": action, "start": START, "end": END}, ctx)
                decide.assert_not_awaited()
            self.assertTrue(ctx.turn_taint.tainted)
        for surface in tool_registry.ALL:
            admin = {s.name: s for s in tool_registry.specs(surface, is_admin=True)}
            self.assertEqual(admin["google_calendar"].effect, tool_registry.EXTERNAL)
            self.assertNotIn("google_calendar", {s.name for s in tool_registry.specs(surface, is_admin=False)})

    async def test_upcoming_tool_includes_google_and_taints_only_admin(self):
        with patch.object(tool_registry.memory_tools, "list_upcoming_events", return_value=[]):
            ctx = tool_registry.ToolContext(is_admin=True, turn_taint=TurnTaint())
            text = await tool_registry._list_upcoming_events({}, ctx)
            self.assertIn("Google calendar: david@example.com", text)
            self.assertTrue(ctx.turn_taint.tainted)
            ctx = tool_registry.ToolContext(turn_taint=TurnTaint())
            self.assertIn("Nothing upcoming", await tool_registry._list_upcoming_events({}, ctx))
            self.assertFalse(ctx.turn_taint.tainted)

    async def test_drive_sections_and_search_are_bounded_queries(self):
        queries = {"my_drive": "'root' in parents", "shared": "sharedWithMe = true", "starred": "starred = true", "trash": "trashed = true"}
        for section, expected in queries.items():
            await google.list_files(section=section)
            self.assertIn(expected, self.requests[-1].url.params["q"])
        await google.list_files(section="recent")
        self.assertEqual(self.requests[-1].url.params["orderBy"], "viewedByMeTime desc")
        await google.list_files(query="x' or trashed = true", section="my_drive")
        self.assertIn("name contains 'x\\' or trashed = true'", self.requests[-1].url.params["q"])
        await google.list_files(parent="folder", section="shared")
        self.assertNotIn("sharedWithMe", self.requests[-1].url.params["q"])
        self.assertIn("'folder' in parents", self.requests[-1].url.params["q"])
        with self.assertRaises(google.GoogleError):
            await google.list_files(section="arbitrary")

    async def test_thumbnail_uses_only_allowlisted_host_and_safe_image_bytes(self):
        result = await self.web("/api/google/drive/files/file/thumbnail")
        self.assertEqual(result.content, PNG)
        self.assertEqual(result.headers["content-type"], "image/png")
        self.assertEqual(self.requests[-1].url.host, "lh3.googleusercontent.com")
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer access")
        for url in ("https://evil/image", "https://lh3.googleusercontent.com.evil/image", "http://lh3.googleusercontent.com/image",
                    "https://user@lh3.googleusercontent.com/image", "https://lh3.googleusercontent.com/%2e%2e/image",
                    "https://lh3.googleusercontent.com//evil", "https://lh3.googleusercontent.com/image#evil"):
            self.info["thumbnailLink"] = url
            self.requests.clear()
            result = await self.web("/api/google/drive/files/file/thumbnail")
            self.assertGreaterEqual(result.status_code, 400, url)
            self.assertEqual(len(self.requests), 1, "only file metadata was fetched")

    async def test_thumbnail_redirect_and_svg_are_rejected(self):
        for response in (httpx.Response(302, headers={"location": "https://evil"}), httpx.Response(200, content=b"<svg onload='evil()'/>", headers={"content-type": "image/svg+xml"})):
            self.response_override = lambda req: response if req.url.host == "lh3.googleusercontent.com" else None
            result = await self.web("/api/google/drive/files/file/thumbnail")
            self.assertGreaterEqual(result.status_code, 400)
            self.assertFalse(any(req.url.host == "evil" for req in self.requests))

    async def test_docs_preview_is_sanitized_on_the_server(self):
        result = await self.web("/api/google/drive/files/file/preview")
        self.assertEqual(result.status_code, 200)
        self.assertIn("text/html", result.headers["content-type"])
        for bad in ("<script", "onerror", "<img", "https://evil", "javascript:"):
            self.assertNotIn(bad, result.text)
        self.assertIn("<h1>Brief</h1>", result.text)
        self.assertIn("sandbox", result.headers["content-security-policy"])
        self.assertEqual(self.requests[-1].url.params["mimeType"], "text/html")

    async def test_preview_types_and_download_cap(self):
        for mime, expected, content in (("application/vnd.google-apps.presentation", "application/pdf", b"%PDF-fixture"),
                                        ("application/pdf", "application/pdf", b"%PDF-fixture"),
                                        ("image/png", "image/png", PNG), ("text/html", "text/plain", b"<script>literal</script>"),
                                        ("application/json", "text/plain", b'{"example": true}'),
                                        ("application/octet-stream", "text/plain", b"print('code')")):
            self.info.update(mimeType=mime, name="example.py")
            self.content = content
            result = await self.web("/api/google/drive/files/file/preview")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.headers["content-type"].startswith(expected))
            self.assertEqual(result.content, content)
        self.info.update(mimeType="application/octet-stream", name="unknown.bin")
        self.assertEqual((await self.web("/api/google/drive/files/file/preview")).status_code, 415)
        self.info.update(mimeType="text/plain", size=str(google.MAX_DOWNLOAD + 1))
        self.requests.clear()
        self.assertEqual((await self.web("/api/google/drive/files/file/preview")).status_code, 413)
        self.assertEqual(len(self.requests), 1)
        self.info.pop("size")
        with patch.object(google, "MAX_DOWNLOAD", 4):
            self.assertEqual((await self.web("/api/google/drive/files/file/preview")).status_code, 413)

    async def test_all_google_routes_are_admin_only(self):
        for method, path, body in (("GET", "/api/google/status", None), ("GET", "/api/google/drive/storage", None),
                                   ("GET", "/api/google/drive/files/file/thumbnail", None), ("GET", "/api/google/drive/files/file/preview", None),
                                   ("GET", "/api/google/calendar/calendars", None), ("GET", f"/api/google/calendar/events?start={START}&end={END}", None),
                                   ("PATCH", "/api/google/calendar/settings", {"calendar_ids": []}),
                                   ("POST", "/api/google/calendar/action", {"action": "delete", "event_id": "timed"})):
            self.assertEqual((await self.web(path, method, body, admin=False)).status_code, 403)
        self.assertFalse(self.requests)


class SanitizingTests(unittest.TestCase):
    def test_resources_attributes_and_unsafe_links_are_stripped(self):
        result = sanitize_html('<style>body{background:url(https://evil)}</style><iframe src="https://evil">bad</iframe>'
                               '<object data="https://evil">bad</object><svg onload="evil()">bad</svg><p class="x" style="color:red" onclick="evil()">Good</p>'
                               '<a href="javascript:evil()">bad link</a><a href="https://evil">remote</a><img src="https://evil" onerror="evil()">'
                               '<table><tr><td colspan="2">Cell</td></tr></table><strong>Text</strong>')
        for bad in ("style", "iframe", "object", "svg", "onload", "onclick", "onerror", "img", "javascript:", "https://", "class="):
            self.assertNotIn(bad, result)
        self.assertIn('<td colspan="2">Cell</td>', result)
        self.assertIn("<strong>Text</strong>", result)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_DATA)
