"""Configured connectors (data/connectors.json). Secrets are stored
encrypted (core/secret_storage) and never returned by the API; the person
sees only whether each one is set."""
import os
import time
import uuid
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import decrypt, encrypt

CONNECTORS_FILE = os.path.join(DATA_DIR, "connectors.json")


def _load() -> dict:
    return read_json(CONNECTORS_FILE, {})


def _save(data: dict) -> None:
    write_json_atomic(CONNECTORS_FILE, data)


def list_records() -> list[dict]:
    return sorted(_load().values(), key=lambda r: r["created_at"])


def get_record(connector_id: str) -> Optional[dict]:
    return _load().get(connector_id)


def secrets_of(record: dict) -> dict:
    return {key: decrypt(value) for key, value in (record.get("secrets") or {}).items() if value}


def public(record: dict, fields: tuple) -> dict:
    """What the API shows: everything but secret values."""
    secret_keys = {f.key for f in fields if f.secret}
    shown = {k: v for k, v in record.items() if k != "secrets"}
    shown["secrets_set"] = {key: bool((record.get("secrets") or {}).get(key)) for key in secret_keys}
    return shown


def _split(values: dict, fields: tuple) -> tuple[dict, dict]:
    settings, secrets = {}, {}
    known = {f.key: f for f in fields}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown setting: {key}")
        (secrets if known[key].secret else settings)[key] = "" if value is None else str(value).strip()
    return settings, secrets


def create(kind: str, name: str, values: dict, fields: tuple, allowed_senders: list, open_to_anyone: bool,
           model_endpoint_id: Optional[str]) -> dict:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("give the connector a name")
    settings, secrets = _split(values, fields)
    missing = [f.label for f in fields if f.required and not (settings.get(f.key) or secrets.get(f.key))]
    if missing:
        raise ValueError(f"missing: {', '.join(missing)}")
    record = {
        "id": uuid.uuid4().hex[:10], "kind": kind, "name": name, "enabled": True,
        "settings": settings, "secrets": {k: encrypt(v) for k, v in secrets.items() if v},
        "allowed_senders": _senders(allowed_senders), "open": bool(open_to_anyone),
        "model_endpoint_id": model_endpoint_id or None, "created_at": time.time(),
    }
    data = _load()
    data[record["id"]] = record
    _save(data)
    return record


def update(connector_id: str, fields: tuple, *, name=None, values=None, allowed_senders=None,
           open_to_anyone=None, model_endpoint_id=..., enabled=None) -> dict:
    data = _load()
    record = data.get(connector_id)
    if record is None:
        raise KeyError(connector_id)
    if name is not None:
        if not " ".join(name.split()):
            raise ValueError("give the connector a name")
        record["name"] = " ".join(name.split())
    if values:
        settings, secrets = _split(values, fields)
        record["settings"].update(settings)
        for key, value in secrets.items():
            if value:  # an empty secret field means "keep the saved one"
                record["secrets"][key] = encrypt(value)
    if allowed_senders is not None:
        record["allowed_senders"] = _senders(allowed_senders)
    if open_to_anyone is not None:
        record["open"] = bool(open_to_anyone)
    if model_endpoint_id is not ...:
        record["model_endpoint_id"] = model_endpoint_id or None
    if enabled is not None:
        record["enabled"] = bool(enabled)
    _save(data)
    return record


def delete(connector_id: str) -> None:
    data = _load()
    data.pop(connector_id, None)
    _save(data)


def _senders(values) -> list[str]:
    seen = []
    for value in values or []:
        value = str(value).strip()
        if value and value not in seen:
            seen.append(value)
    return seen
