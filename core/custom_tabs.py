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

# Premade tabs (David's ask 2026-09-03). Their code ships with the app but
# stays dormant until a user adds one from the New Tab gallery — a fresh
# download shouldn't arrive carrying somebody else's workflow, but rebuilding
# a working tab from scratch just to try it is worse. So: shipped, not
# mounted, one click to switch on.
#
# Each entry is a real tab already built to the routes/tab_<slug>.py
# convention; enabling just makes discover() stop skipping it.
TAB_TEMPLATES = {
    "school": {
        "label": "School",
        "blurb": "Track courses, assignments and due dates, with a per-course chat that remembers what you're working on.",
        "detail": "Built around coursework: add assignments with due dates, keep drafts, and talk to JARVIS about a specific class in a chat that accumulates that course's context.",
    },
}


def template_slugs() -> set[str]:
    return set(TAB_TEMPLATES)


def enabled_templates() -> set[str]:
    return set(settings_store.get_setting("enabled_tab_templates") or [])


def list_templates() -> list[dict]:
    enabled = enabled_templates()
    return [
        {"slug": slug, "enabled": slug in enabled, **meta}
        for slug, meta in sorted(TAB_TEMPLATES.items(), key=lambda kv: kv[1]["label"])
    ]


def set_template_enabled(slug: str, enabled: bool) -> dict:
    if slug not in TAB_TEMPLATES:
        raise KeyError(slug)
    current = enabled_templates()
    if enabled:
        current.add(slug)
    else:
        current.discard(slug)
    settings_store.update_settings(enabled_tab_templates=sorted(current))
    return {"slug": slug, "enabled": enabled}


def discover() -> list[tuple[str, ModuleType]]:
    """(slug, imported module) for every routes/tab_*.py, sorted by slug."""
    found = []
    if not os.path.isdir(ROUTES_DIR):
        return found
    templates = template_slugs()
    enabled = enabled_templates()
    for filename in sorted(os.listdir(ROUTES_DIR)):
        if not (filename.startswith(TAB_MODULE_PREFIX) and filename.endswith(".py")):
            continue
        module_name = filename[:-3]
        slug = module_name[len(TAB_MODULE_PREFIX):]
        if not slug:
            continue
        # A premade tab's code ships with the app but stays invisible until
        # the user adds it from the New Tab gallery.
        if slug in templates and slug not in enabled:
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


def mount_one(app, slug: str) -> bool:
    """Mount a single tab's router into the already-running app, so adding a
    premade tab works immediately instead of "now restart the server."
    FastAPI accepts include_router after startup; the route table is just a
    list. Idempotent — mounting an already-mounted prefix is skipped."""
    try:
        mod = importlib.import_module(f"routes.{TAB_MODULE_PREFIX}{slug}")
    except Exception:
        logger.exception("custom_tabs: couldn't import routes.tab_%s", slug)
        return False
    router = getattr(mod, "router", None)
    if router is None:
        return False
    prefix = getattr(router, "prefix", "")
    if prefix and any(getattr(r, "path", "").startswith(prefix) for r in app.routes):
        return True  # already mounted (e.g. enabled twice in one session)
    app.include_router(router)
    app.openapi_schema = None  # force the schema to regenerate with the new routes
    logger.info("custom_tabs: mounted tab_%s at runtime", slug)
    return True


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
    # A premade tab's files are shipped app files, not something the user
    # created — deleting them would break the gallery and be undone by the
    # next update anyway. Switching it off is the correct "remove".
    if slug in TAB_TEMPLATES:
        set_template_enabled(slug, False)
        return {"removed": [], "disabled": slug}

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
