"""Atomic JSON read/write helpers, shared by auth and any other JSON-backed store.

Odysseus-style: write to a temp file then os.replace() so a crash mid-write
never leaves a corrupt/partial store on disk.
"""
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)


def read_json(path: str, default: Any) -> Any:
    """Read a JSON store, falling back to `default` if it's missing.

    A file that EXISTS but won't parse is treated very differently from one
    that's simply absent, because conflating the two is silently
    destructive: a store that fails to parse returns `default`, the caller
    treats that as real state, and the next routine write persists the
    default — permanently replacing the user's data with empty values, with
    no error anywhere.

    That is not hypothetical. On 2026-09-03 a settings.json written by an
    external tool couldn't be parsed here; the app fell back to DEFAULTS and
    the next update_settings() wrote them to disk, wiping the configured
    vault path — which in turn made vault_sync delete every note it had
    imported, since their source appeared to be gone.

    So an unparseable file is now loud: the bad copy is preserved alongside
    the original with a timestamp, and the failure is logged as an error.
    The caller still gets `default` (the app must boot), but the evidence
    survives and the problem is visible instead of silent.
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            # utf-8-sig, not utf-8: it reads plain UTF-8 unchanged but also
            # tolerates a BOM, which Windows tools (PowerShell's Set-Content,
            # Notepad) prepend by default. A BOM alone used to make an
            # otherwise perfectly valid file unreadable.
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        backup = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = "(couldn't be backed up)"
        logger.error(
            "read_json: %s exists but could not be parsed (%s). A copy was kept at %s. "
            "Falling back to defaults — if this is a settings or credentials store, the "
            "next write will persist those defaults over the unreadable file.",
            path, e, backup,
        )
        return default
    except OSError as e:
        # Unreadable for a non-content reason (locked, permissions). Don't
        # copy it; just make sure it isn't silent.
        logger.error("read_json: couldn't read %s (%s) — falling back to defaults.", path, e)
        return default


def write_json_atomic(path: str, data: Any) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
