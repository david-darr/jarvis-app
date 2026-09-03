"""Generic key/value settings store — Phase 5's backing store for onboarding
state, the chosen vault path, and channel config (Discord token/allowlist),
matching Odysseus's own `src.settings` pattern (one JSON file, defaults
merged underneath). Secret-shaped values (tokens, passwords) are expected to
already be encrypted by the caller via core/secret_storage before they reach
here — this module itself does no encryption/masking of its own.
"""
import os
from typing import Any

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

DEFAULTS: dict[str, Any] = {
    "onboarding_complete": False,
    "vault_dir": None,  # None = use core/vault.py's default resolution
    "discord_bot_token_encrypted": None,
    "discord_allowed_user_id": None,
    "disabled_tools": [],  # Settings > Admin > Agent Tools, David's ask 2026-08-31
    "developer_mode_enabled": False,  # Sidebar toggle, David's ask 2026-09-01
    "custom_tab_order": [],  # Settings > Admin > Custom Tabs, David's ask 2026-09-01
    # Remote access over Tailscale (David's ask 2026-09-03: users should be
    # able to set this up during onboarding the way we run it by hand).
    # See core/remote_access.py.
    "remote_access_enabled": False,
    "remote_access_port": 8422,
    # Turning on real accounts from the UI. AUTH_ENABLED (env) still forces
    # auth on for dev/scripted runs; this is the desktop-app equivalent,
    # since a packaged app has no sensible place for a user to set an env
    # var. Deliberately one-directional in core/auth.py: either source being
    # true enables auth, so this can never be used to switch auth OFF for a
    # deployment that set the env var.
    "auth_enabled": False,
    # Bundled skills already copied into the user's skills folder — tracked
    # per-slug so deleting one doesn't get it resurrected next launch.
    "seeded_skills": [],
    # Premade tabs the user has switched on (David's ask 2026-09-03). The
    # code for these ships with the app but stays unmounted until opted into,
    # so a download doesn't arrive carrying someone else's workflow. See
    # core/custom_tabs.py's TAB_TEMPLATES.
    "enabled_tab_templates": [],
}


def load_settings() -> dict:
    stored = read_json(SETTINGS_FILE, {})
    return {**DEFAULTS, **stored}


def get_setting(key: str) -> Any:
    return load_settings().get(key)


def update_settings(**fields) -> dict:
    current = load_settings()
    for key, value in fields.items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting: {key}")
        current[key] = value
    write_json_atomic(SETTINGS_FILE, current)
    return current
