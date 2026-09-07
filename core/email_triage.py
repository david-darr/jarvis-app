"""Stored result of the daily "which of these emails actually matter" pass.

David's ask 2026-09-06: the Email tab should show the messages JARVIS deems
important, refreshed daily. Kept as its own small store rather than inside
email_service because it is a derived judgement, not account state — blowing
it away and recomputing is always safe, and it must never end up anywhere
near the credential file.

Only headers are stored (sender, subject, date) plus the model's one-line
reason. No message bodies: the triage prompt never fetches them, so there is
nothing here that isn't already visible in an inbox list.
"""
import os
import time
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR

TRIAGE_FILE = os.path.join(DATA_DIR, "email_triage.json")


def load() -> dict:
    """{"generated_at": float|None, "items": [...], "scanned": int, "error": str|None}"""
    return read_json(TRIAGE_FILE, {"generated_at": None, "items": [], "scanned": 0, "error": None})


def save(items: list[dict], scanned: int, error: Optional[str] = None) -> dict:
    payload = {"generated_at": time.time(), "items": items, "scanned": scanned, "error": error}
    write_json_atomic(TRIAGE_FILE, payload)
    return payload
