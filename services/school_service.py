"""School: Canvas assignments grouped by course, each with a saved draft (the
assignment's text editor content) and a persistent per-course chat session
that accumulates real course context over time.

Two independent sync sources, same output shape (course/title/due/url/
description), so the rest of this module and the frontend never care which
one is active:
  - Canvas REST API (preferred when a base URL + API token are configured in
    Settings for this tab — real course names, real assignment descriptions,
    no summary-string parsing needed).
  - Canvas's own per-user iCal feed ("Calendar Feed" link on the Canvas
    Calendar page) — same minimal hand-rolled VEVENT parsing style as
    core/dav_client.py, but that module's parse_vevents() deliberately drops
    URL/DESCRIPTION and has no notion of "course," so this reimplements a
    small Canvas-flavored parser rather than stretching a shared one. Canvas
    formats every feed SUMMARY as "Assignment Title [CourseCode]" — that
    trailing bracket is the only course signal an iCal feed carries at all.

Chat "memory" is intentionally just the normal session_manager conversation
history for a session stable-keyed per course (get_or_create_channel_session,
the same mechanism a Discord channel uses for its one ongoing conversation) —
no separate memory store to keep in sync. sync_course_memory() appends a
context note (course material + current draft) via append_message, which
persists to that history WITHOUT spending a real model turn; the note only
becomes something the model actually reads once the user sends a real
message in that course's chat. Re-opening the same assignment with an
unchanged draft is a no-op (fingerprint check) so browsing around doesn't
spam the transcript with duplicate notes.
"""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt
from core.session_manager import session_manager

SCHOOL_FILE = os.path.join(DATA_DIR, "school.json")
TIMEOUT_SECONDS = 30

_COURSE_SUFFIX_RE = re.compile(r"^(.*)\s\[(.+)\]\s*$")
_TAG_RE = re.compile(r"<[^>]+>")


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _extract_field(block: str, name: str) -> Optional[str]:
    m = re.search(rf"^{name}(?:;[^:\n]*)?:(.+)$", block, re.MULTILINE)
    return m.group(1).strip() if m else None


def _to_iso(dav_dt: str) -> str:
    dav_dt = dav_dt.strip()
    if "T" in dav_dt:
        date_part, time_part = dav_dt.split("T", 1)
        z = time_part.endswith("Z")
        time_part = time_part.rstrip("Z")
        iso = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
        return iso + ("Z" if z else "")
    return f"{dav_dt[0:4]}-{dav_dt[4:6]}-{dav_dt[6:8]}"


def _unescape_ics_text(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _strip_html(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    text = _TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def _extract_attachment_links(a: dict) -> list[str]:
    """Files/readings/rubrics linked *inside* the assignment description —
    separate from `url` (the canonical link to the assignment page itself).
    Neither Canvas's iCal feed nor its REST API expose a real structured
    attachments list without extra per-assignment calls, so this pulls
    plain http(s) links out of the already-fetched description text — a
    real link, not a fabricated one, just found the cheap way. Computed at
    read time (not stored) so a fix here applies to already-synced data."""
    if not a.get("description"):
        return []
    seen = []
    for link in _URL_RE.findall(a["description"]):
        link = link.rstrip(".,;:")
        if link != a.get("url") and link not in seen:
            seen.append(link)
    return seen


def _parse_canvas_ics(ics_text: str) -> list[dict]:
    text = _unfold(ics_text)
    assignments = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL):
        uid = _extract_field(block, "UID")
        summary = _extract_field(block, "SUMMARY")
        dtstart = _extract_field(block, "DTSTART")
        if not (uid and summary and dtstart):
            continue
        url = _extract_field(block, "URL")
        description = _extract_field(block, "DESCRIPTION")
        course, title = "Uncategorized", summary
        m = _COURSE_SUFFIX_RE.match(summary)
        if m:
            title, course = m.group(1).strip(), m.group(2).strip()
        assignments.append({
            "uid": uid,
            "title": title,
            "course": course,
            "due": _to_iso(dtstart),
            "url": url,
            "description": _unescape_ics_text(description) if description else "",
        })
    return assignments


async def _sync_from_ics(ics_url: str) -> list[dict]:
    url = ics_url[len("webcal://"):] if ics_url.startswith("webcal://") else ics_url
    url = "https://" + url if not url.startswith(("http://", "https://")) else url
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _parse_canvas_ics(resp.text)


async def _sync_from_api(base_url: str, token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    assignments = []
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=TIMEOUT_SECONDS) as client:
        courses_resp = await client.get("/api/v1/courses", params={"enrollment_state": "active", "per_page": 100})
        courses_resp.raise_for_status()
        for c in courses_resp.json():
            course_name = c.get("name") or c.get("course_code") or f"Course {c['id']}"
            # One page per course (per_page=100) — simpler than following
            # Link-header pagination, matching core/dav_client.py's stated
            # "simpler than a full client" scope; plenty for a real course load.
            r = await client.get(f"/api/v1/courses/{c['id']}/assignments", params={"per_page": 100, "order_by": "due_at"})
            if r.status_code != 200:
                continue
            for a in r.json():
                assignments.append({
                    "uid": f"canvas-api-{a['id']}",
                    "title": a.get("name") or "Untitled assignment",
                    "course": course_name,
                    "due": a.get("due_at"),
                    "url": a.get("html_url"),
                    "description": _strip_html(a.get("description") or ""),
                })
    return assignments


class SchoolService:
    def __init__(self) -> None:
        self._data: dict = read_json(SCHOOL_FILE, {
            "settings": {}, "assignments": {}, "drafts": {}, "chat_seed_fingerprints": {},
            "last_synced_at": None, "last_sync_source": None,
        })

    def _save(self) -> None:
        write_json_atomic(SCHOOL_FILE, self._data)

    # -- settings ---------------------------------------------------------
    def get_settings(self) -> dict:
        s = self._data.get("settings", {})
        return {
            "canvas_base_url": s.get("canvas_base_url", ""),
            "ics_url": s.get("ics_url", ""),
            "canvas_api_token_configured": bool(s.get("canvas_api_token_encrypted")),
            "last_synced_at": self._data.get("last_synced_at"),
            "last_sync_source": self._data.get("last_sync_source"),
        }

    def update_settings(self, canvas_base_url: Optional[str] = None, canvas_api_token: Optional[str] = None,
                         ics_url: Optional[str] = None) -> dict:
        s = self._data.setdefault("settings", {})
        if canvas_base_url is not None:
            s["canvas_base_url"] = canvas_base_url.strip()
        if canvas_api_token is not None:
            # Empty string clears a previously saved token; never store plaintext.
            s["canvas_api_token_encrypted"] = encrypt(canvas_api_token) if canvas_api_token else None
        if ics_url is not None:
            s["ics_url"] = ics_url.strip()
        self._save()
        return self.get_settings()

    # -- sync ---------------------------------------------------------------
    async def sync(self) -> dict:
        s = self._data.get("settings", {})
        base_url = s.get("canvas_base_url")
        token_encrypted = s.get("canvas_api_token_encrypted")
        ics_url = s.get("ics_url")

        if base_url and token_encrypted:
            fetched = await _sync_from_api(base_url, decrypt(token_encrypted))
            source = "canvas_api"
        elif ics_url:
            fetched = await _sync_from_ics(ics_url)
            source = "ics"
        else:
            raise ValueError("no Canvas API credentials or iCal feed URL configured")

        existing = self._data.setdefault("assignments", {})
        for a in fetched:
            uid = a["uid"]
            prior = existing.get(uid, {})
            existing[uid] = {**a, "id": uid, "completed": prior.get("completed", False)}

        self._data["last_synced_at"] = time.time()
        self._data["last_sync_source"] = source
        self._save()
        return {"count": len(fetched), "source": source, "synced_at": self._data["last_synced_at"]}

    # -- reads ---------------------------------------------------------------
    def list_courses(self) -> list[dict]:
        assignments = list(self._data.get("assignments", {}).values())
        now_iso = datetime.now(timezone.utc).isoformat()
        by_course: dict[str, list[dict]] = {}
        for a in assignments:
            by_course.setdefault(a["course"], []).append(a)
        courses = []
        for name, items in by_course.items():
            open_items = [a for a in items if not a.get("completed")]
            upcoming = [a for a in open_items if a.get("due") and a["due"] >= now_iso]
            overdue = [a for a in open_items if a.get("due") and a["due"] < now_iso]
            courses.append({
                "name": name,
                "assignment_count": len(items),
                "upcoming_count": len(upcoming),
                "overdue_count": len(overdue),
            })
        return sorted(courses, key=lambda c: c["name"])

    def list_assignments(self, course: Optional[str] = None, upcoming_days: Optional[int] = None,
                          overdue: bool = False, include_completed: bool = False) -> list[dict]:
        items = list(self._data.get("assignments", {}).values())
        if course:
            items = [a for a in items if a["course"] == course]
        if not include_completed:
            items = [a for a in items if not a.get("completed")]
        now_iso = datetime.now(timezone.utc).isoformat()
        if overdue:
            items = [a for a in items if a.get("due") and a["due"] < now_iso]
        elif upcoming_days is not None:
            cutoff_iso = (datetime.now(timezone.utc) + timedelta(days=upcoming_days)).isoformat()
            items = [a for a in items if a.get("due") and now_iso <= a["due"] <= cutoff_iso]
        items = sorted(items, key=lambda a: a.get("due") or "9999")
        return [{**a, "attachment_links": _extract_attachment_links(a)} for a in items]

    def get_assignment(self, assignment_id: str) -> Optional[dict]:
        a = self._data.get("assignments", {}).get(assignment_id)
        if a is None:
            return None
        return {**a, "attachment_links": _extract_attachment_links(a)}

    def set_completed(self, assignment_id: str, completed: bool) -> dict:
        a = self._data.get("assignments", {}).get(assignment_id)
        if a is None:
            raise KeyError(f"no such assignment: {assignment_id}")
        a["completed"] = completed
        self._save()
        return a

    # -- drafts (the per-assignment text editor) ------------------------------
    def get_draft(self, assignment_id: str) -> str:
        return self._data.get("drafts", {}).get(assignment_id, "")

    def save_draft(self, assignment_id: str, content: str) -> dict:
        self._data.setdefault("drafts", {})[assignment_id] = content
        self._save()
        return {"ok": True}

    # -- per-course chat memory -----------------------------------------------
    def get_course_session_id(self, course: str) -> str:
        return session_manager.get_or_create_channel_session(f"school:{course}", f"School — {course}")

    def sync_course_memory(self, assignment_id: str) -> Optional[dict]:
        a = self.get_assignment(assignment_id)
        if a is None:
            return None
        draft = self.get_draft(assignment_id)
        session_id = self.get_course_session_id(a["course"])

        fingerprint = f'{a.get("title")}|{a.get("due")}|{a.get("description")}|{draft}'
        seeds = self._data.setdefault("chat_seed_fingerprints", {})
        if seeds.get(assignment_id) == fingerprint:
            return {"session_id": session_id, "updated": False}

        lines = [f'[Course material — {a["course"]}] Assignment: {a["title"]}']
        if a.get("due"):
            lines.append(f'Due: {a["due"]}')
        if a.get("url"):
            lines.append(f'Link: {a["url"]}')
        if a.get("description"):
            lines.append(f'Details: {a["description"]}')
        if draft.strip():
            lines.append(f"Current draft/work so far:\n{draft}")
        session_manager.append_message(session_id, "user", "\n".join(lines))

        seeds[assignment_id] = fingerprint
        self._save()
        return {"session_id": session_id, "updated": True}


school_service = SchoolService()
