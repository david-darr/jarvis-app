"""Custom-tab discovery — Developer Mode (David's ask 2026-09-01: let anyone
who downloads jarvis-app extend it with their own tabs/workflows, built by
their connected AI model using the app's existing conventions, without ever
touching a shared core file).

A custom tab is just a normal routes/*.py module named "tab_<slug>.py" that
additionally exposes a TAB_MANIFEST dict, plus (by the existing, unrelated
loadModule() convention already in static/js/app.js) a matching
static/js/views/<slug>.js. No manifest file, no plugin registry — same
"scan, don't register" spirit as services/skills_service.py's data/skills/
scan, just over routes/ module names instead of a data directory, since a
tab genuinely needs real importable Python (a router, optionally a service).

app.py calls mount_all() once at startup; every tab added after that needs
zero further app.py edits — the actual point, since app.py is a root-level
file outside REPO_CODE_DIRS (core/constants.py) and so isn't part of the
surface a connected model can edit.
"""
import importlib
import logging
import os
from types import ModuleType

from core import settings as settings_store
from core.constants import BASE_DIR

logger = logging.getLogger(__name__)

ROUTES_DIR = os.path.join(BASE_DIR, "routes")
VIEWS_DIR = os.path.join(BASE_DIR, "static", "js", "views")
SERVICES_DIR = os.path.join(BASE_DIR, "services")
TAB_MODULE_PREFIX = "tab_"


def discover() -> list[tuple[str, ModuleType]]:
    """(slug, imported module) for every routes/tab_*.py, sorted by slug."""
    found = []
    if not os.path.isdir(ROUTES_DIR):
        return found
    for filename in sorted(os.listdir(ROUTES_DIR)):
        if not (filename.startswith(TAB_MODULE_PREFIX) and filename.endswith(".py")):
            continue
        module_name = filename[:-3]
        slug = module_name[len(TAB_MODULE_PREFIX):]
        if not slug:
            continue
        try:
            mod = importlib.import_module(f"routes.{module_name}")
        except Exception:
            # A broken custom tab shouldn't take the whole app down — same
            # "degrade gracefully" posture as core/channels/discord_channel.py's
            # own optional-feature handling.
            logger.exception("custom_tabs: failed to import routes.%s, skipping", module_name)
            continue
        found.append((slug, mod))
    return found


def mount_all(app) -> None:
    for slug, mod in discover():
        router = getattr(mod, "router", None)
        if router is None:
            logger.warning("custom_tabs: routes.tab_%s has no `router`, skipping", slug)
            continue
        app.include_router(router)
        logger.info("custom_tabs: mounted tab_%s", slug)


def list_manifests() -> list[dict]:
    manifests = []
    for slug, mod in discover():
        manifest = getattr(mod, "TAB_MANIFEST", None)
        if not manifest:
            continue
        manifests.append({"id": manifest.get("id", slug), "label": manifest.get("label", slug), "icon_svg": manifest.get("icon_svg", "")})

    # Reorderable (David's ask 2026-09-01, Settings > Admin > Custom Tabs) —
    # anything not yet in the saved order (a brand new tab) falls back to
    # alphabetical (discover()'s own sort), appended after everything
    # that's already been explicitly ordered.
    order = settings_store.get_setting("custom_tab_order") or []
    order_index = {slug: i for i, slug in enumerate(order)}
    manifests.sort(key=lambda m: order_index.get(m["id"], len(order)))
    return manifests


def delete(slug: str) -> dict:
    """Removes a custom tab's convention files. No-op-safe on anything
    already missing (matches services/notes_service.py's delete_note style)
    — same shape whether called once or twice. The manifest disappears from
    list_manifests() immediately (discover() re-scans the directory fresh
    on every call, nothing cached), but if the tab registered its own API
    routes those stay reachable until a real restart — Python doesn't
    unmount an already-included FastAPI router at runtime. Same "needs a
    restart" caveat the build-custom-tab Skill already states for *adding*
    a tab, now also true for removing one."""
    removed = []
    candidates = [
        os.path.join(ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py"),
        os.path.join(VIEWS_DIR, f"{slug}.js"),
        os.path.join(SERVICES_DIR, f"{slug}_service.py"),
    ]
    for path in candidates:
        if os.path.exists(path):
            os.remove(path)
            removed.append(os.path.relpath(path, BASE_DIR))
    return {"removed": removed}
