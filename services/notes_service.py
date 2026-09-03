"""Notes: Active Priorities + todos/reminders, unified into one data model —
David's explicit resolution (2026-08-31), not Odysseus's separate notes-only
model. Due-dated notes are read by the Calendar service to render alongside
real events; Notes stays the one source of truth for the item.
"""
import time
import uuid
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
import os

NOTES_FILE = os.path.join(DATA_DIR, "notes.json")


class NotesService:
    def __init__(self) -> None:
        self._notes: dict = read_json(NOTES_FILE, {})

    def _save(self) -> None:
        write_json_atomic(NOTES_FILE, self._notes)

    def list_notes(self, include_completed: bool = True) -> list[dict]:
        items = list(self._notes.values())
        if not include_completed:
            items = [n for n in items if not n["completed"]]
        return sorted(items, key=lambda n: (n["completed"], n.get("due_date") or "", -n["created_at"]))

    def list_due_between(self, start_iso: str, end_iso: str) -> list[dict]:
        """Notes with a due_date falling in [start_iso, end_iso) — used by
        calendar_service to render due-dated notes alongside real events."""
        return [
            n for n in self._notes.values()
            if n.get("due_date") and start_iso <= n["due_date"] < end_iso and not n["completed"]
        ]

    def get_note(self, note_id: str) -> Optional[dict]:
        return self._notes.get(note_id)

    def create_note(self, text: str, due_date: Optional[str] = None, project: str = "personal") -> dict:
        note_id = uuid.uuid4().hex[:12]
        now = time.time()
        note = {
            "id": note_id,
            "text": text,
            "due_date": due_date,  # ISO 8601 string, e.g. "2026-09-01T15:00:00"
            "project": project,
            "completed": False,
            "created_at": now,
            "updated_at": now,
        }
        self._notes[note_id] = note
        self._save()
        return note

    def update_note(self, note_id: str, **fields) -> dict:
        note = self._notes.get(note_id)
        if note is None:
            raise KeyError(f"no such note: {note_id}")
        for key in ("text", "due_date", "project", "completed"):
            if key in fields:
                note[key] = fields[key]
        note["updated_at"] = time.time()
        self._save()
        return note

    def delete_note(self, note_id: str) -> None:
        if note_id in self._notes:
            del self._notes[note_id]
            self._save()


notes_service = NotesService()
