"""Contacts — David's ask 2026-08-31 (add Contacts/CardDAV to Settings >
Integrations). No dedicated Contacts tab exists yet, so this is deliberately
minimal: a flat synced-contacts store, viewable from the Integrations panel
itself rather than a full contacts feature. Real data, honestly scoped
surface — not a stub, just not a whole new tab.
"""
import os

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR

CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.json")


def _load() -> dict:
    return read_json(CONTACTS_FILE, {})


def list_contacts(sync_id: str | None = None) -> list[dict]:
    items = list(_load().values())
    if sync_id:
        items = [c for c in items if c.get("sync_id") == sync_id]
    return sorted(items, key=lambda c: c.get("name", ""))


def replace_synced_contacts(sync_id: str, contacts: list[dict]) -> int:
    data = _load()
    for cid, c in list(data.items()):
        if c.get("sync_id") == sync_id:
            del data[cid]
    for c in contacts:
        cid = f"{sync_id}:{c['uid']}"
        data[cid] = {"id": cid, "name": c["name"], "email": c.get("email"), "phone": c.get("phone"), "sync_id": sync_id}
    write_json_atomic(CONTACTS_FILE, data)
    return len(contacts)
