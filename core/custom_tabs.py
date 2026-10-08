"""Tabs ship in tabs/<slug> or live permanently in data/tabs/<slug>.

Folder tabs have their own package and approval; the older split layout
keeps its absolute imports and whole-tree approvals. Discovery never runs
unapproved source to learn a tab's name. app.py mounts eligible routers at
startup, and enabling a prebuilt tab can mount it immediately.
"""
import importlib
import logging
import os
import re
import shutil
import sys
from types import ModuleType

from core import settings as settings_store, tab_folders
from core.constants import BASE_DIR, DATA_DIR
from core.tab_api import API_VERSION as TAB_API_VERSION

logger = logging.getLogger(__name__)

ROUTES_DIR = os.path.join(BASE_DIR, "routes")
VIEWS_DIR = os.path.join(BASE_DIR, "static", "js", "views")
SERVICES_DIR = os.path.join(BASE_DIR, "services")
PREBUILT_TABS_DIR = os.path.join(BASE_DIR, "tabs")
TAB_MODULE_PREFIX = "tab_"

# Where tabs the USER builds are stored (David's ask 2026-09-03). They used
# to be written into the app's own routes/services/static directories — i.e.
# inside the install folder, which an update replaces wholesale, silently
# destroying anything someone had built. Now they live with the rest of the
# user's data, so updates and reinstalls leave them alone.
#
# Shipped tabs stay in the app directory:
# they're app code, they arrive with each build, and only the enabled/disabled
# choice is user state.
USER_TABS_DIR = os.path.join(DATA_DIR, "tabs")
USER_ROUTES_DIR = os.path.join(USER_TABS_DIR, "routes")
USER_SERVICES_DIR = os.path.join(USER_TABS_DIR, "services")
USER_VIEWS_DIR = os.path.join(USER_TABS_DIR, "views")
# Only these directories participate in the legacy whole-tree approval.
USER_TAB_CODE_DIRS = (USER_ROUTES_DIR, USER_SERVICES_DIR, USER_VIEWS_DIR)
_TAB_SLUG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_APPROVED_FINGERPRINTS_KEY = "approved_custom_tab_fingerprints"
# Keep approvals separate so a legacy hash cannot approve a folder tab.
_FOLDER_APPROVALS_KEY = "approved_folder_tab_fingerprints"

# Browser path the user-tab views are served from (mounted in app.py).
USER_VIEWS_URL = "/custom-views"

_paths_extended = False


def ensure_user_tab_dirs() -> None:
    """Keep the old absolute imports working without rewriting users' tabs.

    Existing Jobs/Minecraft source imports routes.tab_<slug> and
    services.<slug>_service. Extending those packages with ONLY the legacy
    directories preserves that convention; folder tabs never go on either
    search path and use kairos_tabs.<slug> instead.
    """
    global _paths_extended
    for directory in (USER_ROUTES_DIR, USER_SERVICES_DIR, USER_VIEWS_DIR):
        os.makedirs(directory, exist_ok=True)
    if _paths_extended:
        return
    import routes as routes_pkg
    import services as services_pkg
    for pkg, extra in ((routes_pkg, USER_ROUTES_DIR), (services_pkg, USER_SERVICES_DIR)):
        if extra not in pkg.__path__:
            pkg.__path__.append(extra)
    _paths_extended = True


def migrate_user_tabs() -> list[str]:
    """Move any user-authored tab already sitting in the app directory into
    the data directory, once.

    Needed because builds before this change wrote tabs into the install
    folder — those would be destroyed by the very next update. Template tabs
    are skipped: their files are shipped app code, not user work.
    """
    ensure_user_tab_dirs()
    templates = template_slugs()
    moved = []
    if not os.path.isdir(ROUTES_DIR):
        return moved
    for filename in sorted(os.listdir(ROUTES_DIR)):
        if not (filename.startswith(TAB_MODULE_PREFIX) and filename.endswith(".py")):
            continue
        slug = filename[len(TAB_MODULE_PREFIX):-3]
        if not slug or slug in templates:
            continue
        pairs = [
            (os.path.join(ROUTES_DIR, filename), os.path.join(USER_ROUTES_DIR, filename)),
            (os.path.join(VIEWS_DIR, f"{slug}.js"), os.path.join(USER_VIEWS_DIR, f"{slug}.js")),
            (os.path.join(SERVICES_DIR, f"{slug}_service.py"), os.path.join(USER_SERVICES_DIR, f"{slug}_service.py")),
        ]
        try:
            for src, dst in pairs:
                if os.path.exists(src) and not os.path.exists(dst):
                    shutil.move(src, dst)
                elif os.path.exists(src):
                    os.remove(src)  # already migrated; drop the install-dir copy
            moved.append(slug)
        except OSError:
            logger.exception("custom_tabs: couldn't migrate user tab '%s'", slug)
    if moved:
        logger.info("custom_tabs: migrated user tabs to the data directory: %s", ", ".join(moved))
    return moved


def template_slugs() -> set[str]:
    return {e["slug"] for e in _folder_entries() if e["kind"] == "prebuilt"}


def enabled_templates() -> set[str]:
    return set(settings_store.get_setting("enabled_tab_templates") or [])


def list_templates() -> list[dict]:
    templates = {}
    for entry in _folder_entries():
        if entry["kind"] == "prebuilt":
            templates[entry["slug"]] = {"slug": entry["slug"], "label": entry["name"],
                "blurb": entry["blurb"], "detail": entry["detail"], "reads": entry.get("reads", ""), "enabled": entry["enabled"]}
    return sorted(templates.values(), key=lambda e: e["label"])


def set_template_enabled(slug: str, enabled: bool) -> dict:
    if slug not in template_slugs():
        raise KeyError(slug)
    current = enabled_templates()
    if enabled:
        current.add(slug)
    else:
        current.discard(slug)
    settings_store.update_settings(enabled_tab_templates=sorted(current))
    if not enabled:
        tab_folders.unregister_prebuilt(slug)
    return {"slug": slug, "enabled": enabled}


def _user_code_files() -> list[tuple[str, str]]:
    """Every file that can contribute executable user-tab code.

    The same tree fingerprint is attached to every tab approval. This is
    deliberately conservative: an added helper module can be imported by an
    otherwise unchanged tab, so changing any file invalidates all approvals.
    Symlinks fail closed rather than letting a tab import code outside data/tabs.
    """
    files = []
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    if os.path.islink(USER_TABS_DIR) or is_junction(USER_TABS_DIR):
        raise ValueError("custom-tab source root cannot be a symlink or junction")
    for directory in USER_TAB_CODE_DIRS:
        if os.path.islink(directory) or is_junction(directory):
            raise ValueError("custom-tab source directories cannot be symlinks")
        if not os.path.isdir(directory):
            continue
        for root, dirs, names in os.walk(directory, followlinks=False):
            # Bytecode caches are not source, but Python may execute a planted
            # .pyc whose timestamp and size match approved source. Remove
            # them before approval checks and keep Python from recreating them.
            if "__pycache__" in dirs:
                cache_dir = os.path.join(root, "__pycache__")
                if os.path.islink(cache_dir) or is_junction(cache_dir):
                    raise ValueError("custom-tab bytecode cache cannot be a symlink")
                shutil.rmtree(cache_dir)
            dirs[:] = sorted(name for name in dirs if name != "__pycache__")
            if any(os.path.islink(os.path.join(root, name)) or is_junction(os.path.join(root, name))
                   for name in dirs):
                raise ValueError(f"custom-tab source directories cannot contain symlinked folders: {root}")
            for name in sorted(names):
                path = os.path.join(root, name)
                if os.path.islink(path) or is_junction(path) or not os.path.isfile(path):
                    raise ValueError("custom-tab source files cannot be symlinks")
                files.append((os.path.relpath(path, USER_TABS_DIR).replace(os.sep, "/"), path))
    return sorted(files)


def user_code_fingerprint() -> tuple[str, list[str]]:
    """Hash paths and bytes, so adds, removals, renames and edits all revoke approval."""
    return tab_folders.fingerprint_files(_user_code_files())


def _user_tab_slugs() -> list[str]:
    if not os.path.isdir(USER_ROUTES_DIR):
        return []
    slugs = []
    for filename in sorted(os.listdir(USER_ROUTES_DIR)):
        if filename.startswith(TAB_MODULE_PREFIX) and filename.endswith(".py"):
            slug = filename[len(TAB_MODULE_PREFIX):-3]
            if _TAB_SLUG.fullmatch(slug):
                slugs.append(slug)
    return slugs


def _approved_fingerprints() -> dict[str, str]:
    value = settings_store.get_setting(_APPROVED_FINGERPRINTS_KEY) or {}
    return value if isinstance(value, dict) else {}


def user_tab_is_approved(slug: str) -> bool:
    if not _TAB_SLUG.fullmatch(slug):
        return False
    if os.path.lexists(os.path.join(USER_TABS_DIR, slug)):
        entry = _folder_entry(slug, user=True)
        approved = entry is not None and entry["status"] == "on"
        if not approved:
            tab_folders.unregister(slug)
        return approved
    try:
        fingerprint, _ = user_code_fingerprint()
    except (OSError, ValueError):
        logger.exception("custom_tabs: couldn't verify user-tab source fingerprints")
        return False
    return _approved_fingerprints().get(slug) == fingerprint


def _legacy_pending_approvals() -> list[dict]:
    """List tabs whose current executable source tree has not been approved."""
    slugs = _user_tab_slugs()
    if not slugs:
        return []
    try:
        fingerprint, files = user_code_fingerprint()
    except (OSError, ValueError):
        logger.exception("custom_tabs: couldn't fingerprint user-tab source")
        return [{"id": slug, "fingerprint": None, "files": [], "blocked": True}
                for slug in slugs]
    approved = _approved_fingerprints()
    return [{"id": slug, "fingerprint": fingerprint, "files": files,
             "previous_fingerprint": approved.get(slug), "approved": approved.get(slug) == fingerprint,
             "blocked": False}
            for slug in slugs]


def pending_approvals() -> list[dict]:
    folders = [e for e in _folder_entries() if e["kind"] == "user"]
    folder_slugs = {e["slug"] for e in folders}
    pending = [e for e in _legacy_pending_approvals() if e["id"] not in folder_slugs]
    approved = settings_store.get_setting(_FOLDER_APPROVALS_KEY) or {}
    pending += [dict(id=e["slug"], fingerprint=e["fingerprint"], files=e["files"], format="folder",
                     previous_fingerprint=approved.get(e["slug"]), approved=e["status"] == "on",
                     blocked=e["status"] in ("invalid", "needs_newer_kairos"), reason=e["reason"])
                for e in folders]
    return sorted(pending, key=lambda e: e["id"])


def approve_user_tab(slug: str, expected_fingerprint: str) -> dict:
    """Approve only the exact source tree shown to the admin for review."""
    if tab_folders.SLUG.fullmatch(slug) and os.path.lexists(os.path.join(USER_TABS_DIR, slug)):
        entry = _folder_entry(slug, user=True)
        if entry is None:
            raise KeyError(slug)
        if entry["status"] in ("invalid", "needs_newer_kairos"):
            raise ValueError(entry["reason"])
        if not expected_fingerprint or entry["fingerprint"] != expected_fingerprint:
            raise ValueError("custom-tab source changed while approval was pending; review the new files and try again")
        approved = dict(settings_store.get_setting(_FOLDER_APPROVALS_KEY) or {})
        approved[slug] = entry["fingerprint"]
        settings_store.update_settings(**{_FOLDER_APPROVALS_KEY: approved})
        return dict(id=slug, fingerprint=entry["fingerprint"], files=entry["files"], restart_required=True)
    if not _TAB_SLUG.fullmatch(slug) or slug not in _user_tab_slugs():
        raise KeyError(slug)
    current, files = user_code_fingerprint()
    if not expected_fingerprint or current != expected_fingerprint:
        raise ValueError("custom-tab source changed while approval was pending; review the new files and try again")
    approved = dict(_approved_fingerprints())
    approved[slug] = current
    settings_store.update_settings(**{_APPROVED_FINGERPRINTS_KEY: approved})
    return {"id": slug, "fingerprint": current, "files": files, "restart_required": True}


def approved_view_path(slug: str) -> str | None:
    """A view is served only while its owning tab's full source tree is approved."""
    if not _TAB_SLUG.fullmatch(slug) or not user_tab_is_approved(slug):
        return None
    if os.path.lexists(os.path.join(USER_TABS_DIR, slug)):
        return None  # an old view cannot borrow its replacement folder's approval
    path = os.path.join(USER_VIEWS_DIR, f"{slug}.js")
    return path if os.path.isfile(path) and not os.path.islink(path) else None


def approved_view_bytes(slug: str) -> bytes | None:
    """Capture the exact approved view bytes before returning them to a client.

    Recheck the tree after the read to avoid serving bytes from a source tree
    that changed between approval validation and opening the view.
    """
    path = approved_view_path(slug)
    if not path:
        return None
    try:
        before, _ = user_code_fingerprint()
        with open(path, "rb") as handle:
            content = handle.read()
        after, _ = user_code_fingerprint()
    except (OSError, ValueError):
        logger.exception("custom_tabs: couldn't validate user-tab view while serving")
        return None
    if before != after or not user_tab_is_approved(slug):
        return None
    return content


def _folder_entries() -> list[dict]:
    entries = tab_folders.scan(PREBUILT_TABS_DIR, user=False, api_version=TAB_API_VERSION)
    entries += tab_folders.scan(USER_TABS_DIR, user=True, api_version=TAB_API_VERSION)
    for entry in entries:
        _apply_folder_policy(entry)
    return entries


def _folder_entry(slug: str, *, user: bool) -> dict | None:
    if not tab_folders.SLUG.fullmatch(slug):
        return None
    path = os.path.join(USER_TABS_DIR if user else PREBUILT_TABS_DIR, slug)
    if not os.path.lexists(path):
        return None
    entry = tab_folders.inspect_folder(path, user=user, api_version=TAB_API_VERSION)
    _apply_folder_policy(entry)
    return entry


def _apply_folder_policy(entry: dict) -> None:
    user = entry["kind"] == "user"
    entry["enabled"] = None if user else entry["slug"] in enabled_templates()
    if entry["status"] is None:
        approved = settings_store.get_setting(_FOLDER_APPROVALS_KEY) or {}
        entry["status"] = ("on" if approved.get(entry["slug"]) == entry["fingerprint"]
                           else "needs_approval") if user else ("on" if entry["enabled"] else "off")


def _folder_allowed(entry: dict) -> bool:
    # Check only this tab. Overrides are policy, even before approval.
    slug = entry["slug"]
    user = entry["kind"] == "user"
    snapshot = entry.get("_snapshot")
    if (entry["status"] != "on" or not snapshot
            or entry["_path"] != os.path.join(USER_TABS_DIR if user else PREBUILT_TABS_DIR, slug)):
        return False
    if user:
        if (settings_store.get_setting(_FOLDER_APPROVALS_KEY) or {}).get(slug) != snapshot:
            return False
    elif slug not in enabled_templates():
        return False
    if entry["kind"] == "prebuilt" and (os.path.lexists(os.path.join(USER_TABS_DIR, slug))
            or os.path.isfile(os.path.join(USER_ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py"))):
        return False
    try:
        # The captured metadata was already validated. One hash of this tab
        # suffices; stat-only caches can miss same-size, timestamp-preserving edits.
        return tab_folders.fingerprint(entry["_path"], user=user)[0] == snapshot
    except (OSError, ValueError):
        return False


def _entry_allowed(entry: dict) -> bool:
    if entry["format"] != "legacy":
        return _folder_allowed(entry)
    slug = entry["slug"]
    fingerprint = entry["fingerprint"]
    if (os.path.lexists(os.path.join(USER_TABS_DIR, slug))
            or not os.path.isfile(os.path.join(USER_ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py"))
            or _approved_fingerprints().get(slug) != fingerprint):
        return False
    try:
        # Legacy helpers are shared, so their existing approval covers the tree.
        return user_code_fingerprint()[0] == fingerprint
    except (OSError, ValueError):
        return False


def _entry_version(entry: dict) -> tuple:
    return (entry["kind"], entry["format"], entry.get("_path"),
            entry.get("_snapshot"), entry.get("fingerprint"))


def _candidates() -> dict[str, dict]:
    folders = _folder_entries()
    candidates = {}
    for entry in folders:
        if entry["kind"] == "prebuilt":
            candidates[entry["slug"]] = entry
    # Do not import legacy source to inspect it until the full tree is approved.
    legacy = {e["id"]: e for e in _legacy_pending_approvals()}
    for slug in _user_tab_slugs():
        approval = legacy[slug]
        candidates[slug] = dict(slug=slug, name=slug, description="", version="", blurb="", detail="",
            kind="user", format="legacy", enabled=None,
            status="invalid" if approval["blocked"] else ("on" if approval["approved"] else "needs_approval"),
            reason="could not verify legacy source tree" if approval["blocked"] else None,
            fingerprint=approval["fingerprint"], files=approval["files"])
    # A folder is the new format; if both user formats exist, the folder wins.
    # Even an invalid/unapproved user override keeps the shipped tab dormant.
    for entry in folders:
        if entry["kind"] == "user":
            candidates[entry["slug"]] = entry
    return candidates


def _load_candidates() -> tuple[dict[str, dict], list[tuple[str, ModuleType]]]:
    ensure_user_tab_dirs()
    candidates = _candidates()
    tab_folders.register_all([e for e in candidates.values() if e["format"] != "legacy"], _folder_allowed)
    found = []
    for slug, entry in sorted(candidates.items()):
        if entry["status"] != "on":
            continue
        try:
            if entry["format"] != "legacy":
                mod = tab_folders.import_routes(entry)
                if not _folder_allowed(entry):
                    raise ImportError("tab source changed while importing")
            else:
                # The legacy package paths remain intact for absolute helper
                # imports. Explicitly choose the user's route on a slug clash.
                name = f"routes.{TAB_MODULE_PREFIX}{slug}"
                route_path = os.path.join(USER_ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py")
                mod = sys.modules.get(name)
                if mod is None or getattr(mod, "__file__", None) != route_path:
                    spec = importlib.util.spec_from_file_location(name, route_path)
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[name] = mod
                    previous = sys.dont_write_bytecode
                    sys.dont_write_bytecode = True
                    try:
                        spec.loader.exec_module(mod)
                    except Exception:
                        sys.modules.pop(name, None)
                        raise
                    finally:
                        sys.dont_write_bytecode = previous
            from fastapi import APIRouter
            if not isinstance(getattr(mod, "router", None), APIRouter):
                raise ValueError("routes.py must expose an APIRouter named router")
            mod._jarvis_is_user_tab = entry["kind"] == "user"
            # Cached legacy modules still contain their original code. Never
            # relabel them with a newly approved fingerprint during discovery.
            if entry["format"] != "legacy" or not hasattr(mod, "_jarvis_tab_entry"):
                mod._jarvis_tab_entry = dict(entry)
            if entry["format"] == "legacy":
                manifest = getattr(mod, "TAB_MANIFEST", {})
                entry["name"] = manifest.get("label", entry["name"])
            found.append((slug, mod))
        except Exception as exc:
            entry.update(status="failed", reason=str(exc))
            if entry["format"] != "legacy":
                tab_folders.unregister(slug)
            logger.exception("custom_tabs: failed to load tab '%s', skipping", slug)
    return candidates, found


def discover() -> list[tuple[str, ModuleType]]:
    return _load_candidates()[1]


def mount_all(app) -> None:
    for slug, mod in discover():
        _mount(app, slug, mod)


def _mount(app, slug: str, mod: ModuleType) -> bool:
    # Track the tab slug instead of prefix collisions: another tab may
    # legitimately share a prefix, and an empty prefix is valid too.
    mounted = getattr(app.state, "custom_tab_modules", {})
    entries = getattr(app.state, "custom_tab_entries", {})
    entry = dict(mod._jarvis_tab_entry)
    if slug in mounted:
        return _entry_version(entries[slug]) == _entry_version(entry) and _entry_allowed(entries[slug])
    if not _entry_allowed(entry):
        return False
    from fastapi import Depends, HTTPException
    async def active():
        import asyncio
        allowed = await asyncio.to_thread(_entry_allowed, entry)
        if not allowed:
            reason = "This tab was removed or changed. Restart Kairos to load the current version."
            if entry["kind"] == "prebuilt" and slug not in enabled_templates():
                reason = "This tab is off."
            raise HTTPException(409, reason)
    owned = getattr(app.state, "custom_tab_routes", {})
    before = {id(route) for route in app.router.routes}
    app.include_router(mod.router, dependencies=[Depends(active)])
    owned[slug] = [route for route in app.router.routes if id(route) not in before]
    app.state.custom_tab_routes = owned
    mounted[slug] = mod
    app.state.custom_tab_modules = mounted
    entries[slug] = entry
    app.state.custom_tab_entries = entries
    app.openapi_schema = None
    from core import tab_hooks
    tab_hooks.request_refresh()
    logger.info("custom_tabs: mounted tab '%s'", slug)
    return True


def mount_one(app, slug: str) -> bool:
    """Enable a shipped router immediately, with the same policy as startup."""
    for candidate, mod in discover():
        if candidate == slug:
            return _mount(app, slug, mod)
    return False


def list_tabs() -> list[dict]:
    candidates, _ = _load_candidates()
    entries = list(candidates.values())
    # Keep shadowed prebuilts visible to the Tool Store, even though the
    # user's override alone supplies routes and sidebar metadata.
    for entry in _folder_entries():
        winner = candidates[entry["slug"]]
        if (winner["kind"], winner["format"]) != (entry["kind"], entry["format"]):
            if entry["status"] == "on":
                entry.update(status="off", reason="overridden by a user tab")
            entries.append(entry)
    return [{key: value for key, value in entry.items() if not key.startswith("_")}
            for entry in sorted(entries, key=lambda e: (e["slug"], e["kind"]))]


def folder_file_bytes(slug: str, filename: str) -> bytes | None:
    if not tab_folders.SLUG.fullmatch(slug):
        return None
    user = os.path.lexists(os.path.join(USER_TABS_DIR, slug))
    if not user and os.path.isfile(os.path.join(USER_ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py")):
        return None
    entry = _folder_entry(slug, user=user)
    if entry is None or entry["format"] == "legacy" or entry["status"] != "on":
        return None
    return tab_folders.file_bytes(entry, filename, _folder_allowed)


def list_manifests() -> list[dict]:
    manifests = []
    for slug, mod in discover():
        entry = mod._jarvis_tab_entry
        user = entry["kind"] == "user"
        if entry["format"] == "legacy":
            manifest = getattr(mod, "TAB_MANIFEST", None)
            if not manifest:
                continue
            view_url = f"{USER_VIEWS_URL}/{slug}.js" if user else None
            style_url = None
        else:
            meta = entry["_metadata"]
            manifest = dict(id=slug, label=meta["name"], icon_svg=meta.get("icon_svg", ""))
            view_url = f"{tab_folders.FILES_URL}/{slug}/view.js" if "view.js" in entry["files"] else None
            style_url = f"{tab_folders.FILES_URL}/{slug}/view.css" if "view.css" in entry["files"] else None
        manifests.append(dict(id=manifest.get("id", slug), label=manifest.get("label", slug),
            icon_svg=manifest.get("icon_svg", ""), view_url=view_url, style_url=style_url,
            user_tab=user, format=entry["format"]))
    return manifests


def delete(slug: str) -> dict:
    """Remove user source, or disable a shipped tab, preserving tab data.

    Already-mounted routes refuse requests after removal. Their routing
    entries disappear at restart.
    """
    if not _TAB_SLUG.fullmatch(slug):
        raise ValueError("invalid tab slug")
    folder = os.path.join(USER_TABS_DIR, slug)
    if os.path.lexists(folder):
        # Verify the whole tree before recursively deleting it, including
        # Windows junctions; never follow a link out of the user's tab root.
        tab_folders.code_files(folder, user=True)
        shutil.rmtree(folder)
        tab_folders.unregister(slug)
        approved = dict(settings_store.get_setting(_FOLDER_APPROVALS_KEY) or {})
        approved.pop(slug, None)
        settings_store.update_settings(**{_FOLDER_APPROVALS_KEY: approved})
        return {"removed": [slug], "restart_required": True}
    # A premade tab's files are shipped app files, not something the user
    # created — deleting them would break the Tool Store and be undone by the
    # next update anyway. Switching it off is the correct "remove".
    if slug in template_slugs() and slug not in _user_tab_slugs():
        set_template_enabled(slug, False)
        return {"removed": [], "disabled": slug, "restart_required": True}

    ensure_user_tab_dirs()
    removed = []
    # Both locations: the data directory is where user tabs live now, and the
    # app directory is checked too so a tab created by an older build (before
    # they moved) can still be deleted.
    candidates = [
        os.path.join(USER_ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py"),
        os.path.join(USER_VIEWS_DIR, f"{slug}.js"),
        os.path.join(USER_SERVICES_DIR, f"{slug}_service.py"),
    ]
    if slug not in template_slugs():
        candidates += [os.path.join(ROUTES_DIR, f"{TAB_MODULE_PREFIX}{slug}.py"),
                       os.path.join(VIEWS_DIR, f"{slug}.js"),
                       os.path.join(SERVICES_DIR, f"{slug}_service.py")]
    for path in candidates:
        if os.path.exists(path):
            os.remove(path)
            removed.append(os.path.basename(path))
    approved = dict(_approved_fingerprints())
    if slug in approved:
        del approved[slug]
        settings_store.update_settings(**{_APPROVED_FINGERPRINTS_KEY: approved})
    return {"removed": removed, "restart_required": True}
