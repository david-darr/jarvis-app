"""Commit-pinned, bounded community catalog and existing-gate installation."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from core import store_schema
from core.atomic_io import read_json, write_json_atomic
from core.constants import BASE_DIR, DATA_DIR

REPO = "david-darr/kairos-store"
HEAD_URL = f"https://api.github.com/repos/{REPO}/commits/main"
HOSTS = {"raw.githubusercontent.com", "api.github.com"}
TTL = 3600
MAX_INDEX = store_schema.MAX_INDEX_BYTES


def _kairos_version():
    version = os.environ.get("KAIROS_VERSION")
    if version is not None:
        return version
    try:
        return json.loads((Path(BASE_DIR) / "electron" / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("Cannot determine Kairos version; installation refused. KAIROS_VERSION is unset and electron/package.json is unavailable or invalid.") from exc


def allowed_url(url):
    u = urlsplit(url)
    if u.scheme != "https" or u.netloc not in HOSTS or u.username or u.password or u.query or u.fragment:
        raise ValueError("Store downloads require an allowlisted HTTPS GitHub host")
    return url


async def fetch_bytes(url, limit):
    allowed_url(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url, headers={"Accept": "application/vnd.github+json"}) as response:
            if response.status_code in (403, 429):
                raise ValueError("GitHub rate limit reached. The saved catalog is still available; try refreshing later.")
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("Store response exceeds size limit")
    return bytes(data)


def validate_index(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "generated_from_commit", "items"} or type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["generated_from_commit"], str) or not isinstance(value["items"], list) or len(value["items"]) > 1000:
        raise ValueError("Invalid store catalog")
    seen = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"kind", "slug", "name", "description", "author", "version", "path", "archive_sha256", "files"}:
            raise ValueError("Invalid catalog item fields")
        store_schema.manifest({**{k: item[k] for k in ("kind", "slug", "name", "description", "author", "version", "files")}, "license": "MIT"})
        key = (item["kind"], item["slug"])
        if key in seen or item["path"] != f"items/{item['kind']}/{item['slug']}" or not isinstance(item["archive_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["archive_sha256"]):
            raise ValueError("Invalid catalog path, hash or duplicate item")
        seen.add(key)
    return value


def _key(kind, slug):
    if not isinstance(kind, str) or kind not in store_schema.KINDS or not isinstance(slug, str) or not store_schema.SLUG.fullmatch(slug):
        raise ValueError("Invalid store kind or slug")
    return kind + "/" + slug


class StoreCatalog:
    def __init__(self, data_dir=DATA_DIR):
        self.root = Path(data_dir)
        self.cache_path = self.root / "store-catalog.json"
        self.installs_path = self.root / "store-installs.json"
        self.lock = asyncio.Lock()

    def installs(self):
        return read_json(str(self.installs_path), {})

    def save_installs(self, value):
        write_json_atomic(str(self.installs_path), value)

    def raw_url(self, commit, path):
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Invalid pinned GitHub commit")
        store_schema.safe_path(path)
        return f"https://raw.githubusercontent.com/{REPO}/{commit}/{quote(path, safe='/')}"

    def _cache(self):
        value = read_json(str(self.cache_path), None)
        if value is not None:
            try:
                self.raw_url(value["commit"], "index.json")
                validate_index(value["index"])
                store_schema.revocations(value["revoked"])
                if not isinstance(value["fetched_at"], (int, float)):
                    raise ValueError("Invalid cache time")
            except (KeyError, TypeError, ValueError):
                return None
        return value

    async def snapshot(self, refresh=False):
        async with self.lock:
            cached = self._cache()
            if cached and not refresh and time.time() - cached["fetched_at"] < TTL:
                return cached
            try:
                head = json.loads(await fetch_bytes(HEAD_URL, 100_000))
                commit = head.get("sha")
                index_url = self.raw_url(commit, "index.json")
                index = validate_index(json.loads(await fetch_bytes(index_url, MAX_INDEX)))
                revoked = store_schema.revocations(json.loads(await fetch_bytes(self.raw_url(commit, "revoked.json"), MAX_INDEX)))
                value = {"commit": commit, "index": index, "revoked": revoked, "fetched_at": time.time()}
                write_json_atomic(str(self.cache_path), value)
                return value
            except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError) as exc:
                if cached:
                    return {**cached, "stale": True, "error": str(exc)}
                raise ValueError(f"Community catalog unavailable: {exc}") from exc

    def _exists(self, record):
        from core import custom_tabs, integrations
        from services import skills_service
        from services.task_service import task_service
        kind, ident = record["kind"], record["local_id"]
        if kind == "tab":
            return (Path(custom_tabs.USER_TABS_DIR) / ident / "tab.json").is_file()
        if kind == "skill":
            return Path(skills_service._skill_path(ident)).is_file()
        if kind == "tool":
            return integrations.get_integration(ident) is not None
        return task_service.get_task(ident) is not None

    async def _enabled(self, record, enabled):
        from core import custom_tabs, integrations, tab_hooks
        from services import skill_curator
        from services.task_service import task_service
        ident = record["local_id"]
        if record["kind"] == "tab":
            custom_tabs.set_user_tab_enabled(ident, enabled)
            await tab_hooks.reconcile()
        elif record["kind"] == "skill":
            skill_curator.set_enabled(ident, enabled)
        elif record["kind"] == "tool":
            integrations.set_enabled(ident, enabled)
        else:
            task_service.update_task(ident, enabled=enabled)

    async def reconcile(self, snapshot):
        records = self.installs()
        changed = False
        for key, record in list(records.items()):
            if not self._exists(record):
                del records[key]; changed = True
                continue
            matches = [r for r in snapshot["revoked"]["items"] if r["kind"] == record["kind"] and r["slug"] == record["slug"] and (r["versions"] == "all" or record["version"] in r["versions"])]
            if matches:
                token = hashlib.sha256(json.dumps(matches, sort_keys=True).encode()).hexdigest()
                reason = "\n".join(r["reason"] for r in matches)
                if record.get("revocation_ack") != token:
                    # Repeat disabling on reads too, so ordinary approval cannot bypass suspension.
                    await self._enabled(record, False)
                    if record.get("revocation_token") != token or not record.get("turned_off"):
                        record.update(revocation_token=token, revoked_reason=reason, turned_off=True)
                        changed = True
            # Keep a pulled install off until the person explicitly restores it.
        if changed:
            self.save_installs(records)
        return records

    async def catalog(self, refresh=False):
        snapshot = await self.snapshot(refresh)
        async with self.lock:
            records = await self.reconcile(snapshot)
            items = []
            keys = set()
            for item in snapshot["index"]["items"]:
                key = _key(item["kind"], item["slug"]); keys.add(key)
                rec = records.get(key)
                revoked = [r for r in snapshot["revoked"]["items"] if r["kind"] == item["kind"] and r["slug"] == item["slug"] and (r["versions"] == "all" or item["version"] in r["versions"])]
                items.append({**item, "installed": bool(rec), "installed_version": rec["version"] if rec else None,
                              "update_available": bool(rec and rec["version"] != item["version"]),
                              "installed_commit": (rec or {}).get("commit"),
                              "turned_off": bool(rec and rec.get("turned_off")),
                              "revoked_reason": (rec or {}).get("revoked_reason") or "\n".join(r["reason"] for r in revoked),
                              "install_blocked": bool(revoked), "local_id": (rec or {}).get("local_id")})
            # Withdrawn items remain visible so Remove/Turn back on stay available.
            for key, rec in records.items():
                if key not in keys:
                    items.append({**rec, "name": rec.get("name", rec["slug"]), "description": rec.get("description", "Removed from the community catalog."), "author": rec.get("author", ""), "installed": True, "update_available": False, "install_blocked": True})
            return {"items": items, "commit": snapshot["commit"], "stale": snapshot.get("stale", False), "error": snapshot.get("error")}

    async def download(self, item, commit):
        files = {}
        _, total_limit, single_limit = store_schema.LIMITS[item["kind"]]
        total = 0
        for name in ["manifest.json", *item["files"]]:
            limit = 16_384 if name == "manifest.json" else (100_000 if name == "SKILL.md" else single_limit)
            content = await fetch_bytes(self.raw_url(commit, item["path"] + "/" + store_schema.safe_path(name)), limit)
            total += len(content)
            if total > total_limit + 16_384:
                raise ValueError("Store item exceeds total size limit")
            files[name] = content
        if store_schema.archive_sha256(files) != item["archive_sha256"]:
            raise ValueError("Store archive_sha256 mismatch; installation refused")
        meta = store_schema.manifest(json.loads(files["manifest.json"]), item["kind"], item["slug"])
        for key in ("name", "description", "author", "version", "files"):
            if meta[key] != item[key]:
                raise ValueError("Store manifest differs from catalog")
        store_schema.validate_bytes(meta, files)
        if meta.get("min_kairos_version"):
            current = _kairos_version()
            if store_schema.semver_key(current) < store_schema.semver_key(meta["min_kairos_version"]):
                raise ValueError("This item needs a newer Kairos")
        return files

    async def install(self, kind, slug, *, confirmed=False, expected_fingerprint=None, expected_sha256=None, replace=False, expected_commit=None):
        from core import custom_tabs, tab_install, tab_hooks
        from routes import integrations_routes
        from services import skills_service, skill_curator
        from services.task_service import task_service
        key = _key(kind, slug)
        snapshot = await self.snapshot()
        if confirmed and expected_commit != snapshot["commit"]:
            raise ValueError("The catalog changed since review. Start the install again.")
        item = next((i for i in snapshot["index"]["items"] if i["kind"] == kind and i["slug"] == slug), None)
        if item is None:
            raise ValueError("Store item not found")
        if any(r["kind"] == kind and r["slug"] == slug and (r["versions"] == "all" or item["version"] in r["versions"]) for r in snapshot["revoked"]["items"]):
            raise ValueError("This store version has been revoked")
        files = await self.download(item, snapshot["commit"])
        async with self.lock:
            policy = self._cache() or snapshot
            if any(r["kind"] == kind and r["slug"] == slug and (r["versions"] == "all" or item["version"] in r["versions"]) for r in policy["revoked"]["items"]):
                raise ValueError("This store version has been revoked")
            records = await self.reconcile(policy)
            old = records.get(key)
            if old and not replace:
                raise ValueError("Already installed; choose update explicitly")
            if replace and not old:
                raise ValueError("Only an installed store item may be updated")
            try:
                if kind == "tab":
                    result = await asyncio.to_thread(tab_install.install, store_schema.tab_archive(slug, files), replace=bool(old), confirmed=confirmed, expected_fingerprint=expected_fingerprint)
                    local_id = result["slug"]
                    await tab_hooks.reconcile()
                elif kind == "skill":
                    if confirmed and expected_sha256 != item["archive_sha256"]:
                        raise ValueError("The skill changed since review. Start again.")
                    result = await asyncio.to_thread(skills_service.import_skill, "SKILL.md", files["SKILL.md"].decode("utf-8-sig").replace("\r\n", "\n"), confirmed=confirmed, name=slug,
                        origin=self.raw_url(snapshot["commit"], item["path"] + "/SKILL.md"), replace=bool(old), supporting_files={n: files[n] for n in item["files"] if n != "SKILL.md"})
                    local_id = result["slug"]
                elif kind == "tool":
                    server = json.loads(files["server.json"])
                    body = integrations_routes.CreateMcpServerRequest(name=server["name"], mcp_type=server["transport"], url=server["url"], auth="oauth" if server["auth_type"] == "oauth" else None)
                    result = await integrations_routes.add_mcp_server(body, replace_id=old["local_id"] if old else None)
                    local_id = result["id"]
                else:
                    template = json.loads(files["automation.json"])
                    result = task_service.create_task(name=template["title"], prompt=template["prompt"], **template["schedule"], enabled=False)
                    local_id = result["id"]
                    if old:
                        task_service.delete_task(old["local_id"])
            except tab_install.ReviewRequired as exc:
                exc.detail["commit"] = snapshot["commit"]
                raise
            except skill_curator.SkillImportRefused as exc:
                exc.sha256 = item["archive_sha256"]
                exc.commit = snapshot["commit"]
                raise
            records[key] = {**{k: item[k] for k in ("kind", "slug", "version", "name", "description", "author")}, "commit": snapshot["commit"], "local_id": local_id, "turned_off": False}
            if kind == "automation" and template.get("model_hint"):
                records[key]["model_hint"] = template["model_hint"]
            # Updating never implicitly undoes a prior store suspension.
            if old and old.get("turned_off"):
                records[key].update(turned_off=True, revoked_reason=old.get("revoked_reason"))
                await self._enabled(records[key], False)
            self.save_installs(records)
            return {**result, "commit": snapshot["commit"]}

    async def reenable(self, kind, slug, confirmed=False, expected_revocation=None):
        if not confirmed:
            raise ValueError("Explicit confirmation is required to turn a revoked item back on")
        snapshot = await self.snapshot()
        async with self.lock:
            records = await self.reconcile(snapshot)
            record = records.get(_key(kind, slug))
            if not record or not record.get("turned_off"):
                raise ValueError("No turned-off store install found")
            if expected_revocation != record.get("revoked_reason"):
                raise ValueError("The revocation changed; review its current reason")
            await self._enabled(record, True)
            record.update(turned_off=False, revocation_ack=record.get("revocation_token"))
            self.save_installs(records)
            return {"ok": True, "local_id": record["local_id"]}

    async def remove(self, kind, slug):
        from core import custom_tabs, integrations, tab_hooks
        from services import skills_service
        from services.task_service import task_service
        async with self.lock:
            records = self.installs(); key = _key(kind, slug)
            rec = records.get(key)
            if rec:
                ident = rec["local_id"]
                if kind == "tab":
                    custom_tabs.delete(ident); await tab_hooks.reconcile()
                elif kind == "skill":
                    skills_service.delete_skill(ident)
                elif kind == "tool":
                    integrations.delete_integration(ident)
                else:
                    task_service.delete_task(ident)
                records.pop(key)
                self.save_installs(records)
            return {"ok": True}


store_catalog = StoreCatalog()


async def watch_revocations():
    """Refresh installed items hourly while Kairos runs, including off-screen."""
    while True:
        try:
            if store_catalog.installs():
                await store_catalog.catalog()
        except (ValueError, OSError, KeyError):
            logging.getLogger(__name__).exception("Community revocation refresh failed")
        await asyncio.sleep(TTL)


_watch = None


def start_watch():
    global _watch
    if _watch is None or _watch.done():
        _watch = asyncio.create_task(watch_revocations())


async def stop_watch():
    global _watch
    task, _watch = _watch, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
