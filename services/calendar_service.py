"""Calendar: real events, plus a read-through view of Notes' due-dated items
(Notes stays the one source of truth for those — see JARVIS Plan's resolved
Notes/Calendar decision, 2026-08-31). External CalDAV/Google sync is real
Odysseus-scale integration work (OAuth, two-way writeback) — deliberately out
of this pass; this is the local event model + Notes-merge it would sync on
top of.
"""
import os
import time
import uuid
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from services.notes_service import notes_service

EVENTS_FILE = os.path.join(DATA_DIR, "calendar_events.json")


class CalendarService:
    def __init__(self) -> None:
        self._events: dict = read_json(EVENTS_FILE, {})

    def _save(self) -> None:
        write_json_atomic(EVENTS_FILE, self._events)

    def create_event(self, title: str, start: str, end: str, all_day: bool = False, location: str = "", description: str = "",
                      sync_id: Optional[str] = None, external_uid: Optional[str] = None, completed: bool = False) -> dict:
        event_id = uuid.uuid4().hex[:12]
        now = time.time()
        event = {
            "id": event_id,
            "title": title,
            "start": start,  # ISO 8601
            "end": end,
            "all_day": all_day,
            "location": location,
            "description": description,
            "created_at": now,
            "updated_at": now,
            # Set when this event came from a CalDAV integration sync
            # (David's ask 2026-08-31, see core/dav_client.py) rather than
            # being hand-created — lets replace_synced_events() clear and
            # re-insert just that integration's events on each re-sync
            # without touching manually created ones.
            "sync_id": sync_id,
            # The feed's own UID (from core/dav_client.py's parse_vevents) —
            # a synced event's real id changes on every re-sync (delete +
            # recreate), so this is the stable key replace_synced_events()
            # uses to carry a "completed" checkbox forward across re-syncs.
            "external_uid": external_uid,
            # Check off an event/note in Calendar (David's ask 2026-08-31) —
            # completed items stay visible (checked, struck through) rather
            # than being deleted, same "mark done, don't destroy" model
            # Notes already uses.
            "completed": completed,
        }
        self._events[event_id] = event
        self._save()
        return event

    def replace_synced_events(self, sync_id: str, events: list[dict]) -> int:
        """Clears every event previously synced from `sync_id`, inserts the
        freshly fetched set. Simplest-correct re-sync model for a one-way
        read sync with no stable diffing — matches core/dav_client.py's
        stated "no incremental sync" scope. Preserves a checked-off
        `completed` state across the wipe-and-recreate by matching on the
        feed's own UID, so re-syncing a CalDAV/iCal calendar doesn't
        silently un-check anything you'd already marked done."""
        completed_by_uid = {
            e["external_uid"]: True
            for e in self._events.values()
            if e.get("sync_id") == sync_id and e.get("external_uid") and e.get("completed")
        }
        for event_id, e in list(self._events.items()):
            if e.get("sync_id") == sync_id:
                del self._events[event_id]
        for e in events:
            uid = e.get("uid")
            self.create_event(
                e["title"], e["start"], e["end"], e.get("all_day", False),
                sync_id=sync_id, external_uid=uid, completed=completed_by_uid.get(uid, False),
            )
        self._save()
        return len(events)

    def update_event(self, event_id: str, **fields) -> dict:
        event = self._events.get(event_id)
        if event is None:
            raise KeyError(f"no such event: {event_id}")
        for key in ("title", "start", "end", "all_day", "location", "description", "completed"):
            if key in fields:
                event[key] = fields[key]
        event["updated_at"] = time.time()
        self._save()
        return event

    def delete_event(self, event_id: str) -> None:
        if event_id in self._events:
            del self._events[event_id]
            self._save()

    def list_completed(self) -> list[dict]:
        """Archived (checked-off) calendar events, most recently completed
        first — David's ask 2026-08-31, an Archive view with a reopen
        action. Not date-range-limited on purpose: a completed event should
        stay findable in the archive regardless of when it was scheduled."""
        events = [{**e, "source": "calendar"} for e in self._events.values() if e.get("completed")]
        return sorted(events, key=lambda e: e["updated_at"], reverse=True)

    def list_range(self, start_iso: str, end_iso: str) -> list[dict]:
        """Real events plus due-dated Notes in range, tagged by source so the
        UI can render/link them differently while Notes stays authoritative
        for the note-backed ones."""
        events = [
            {**e, "source": "calendar"}
            for e in self._events.values()
            if e["start"] < end_iso and e["end"] >= start_iso
        ]
        note_events = [
            {
                "id": n["id"],
                "title": n["text"],
                "start": n["due_date"],
                "end": n["due_date"],
                "all_day": False,
                "location": "",
                "description": "",
                "source": "note",
                "completed": False,  # list_due_between already excludes completed notes
            }
            for n in notes_service.list_due_between(start_iso, end_iso)
        ]
        return sorted(events + note_events, key=lambda e: e["start"])


calendar_service = CalendarService()
