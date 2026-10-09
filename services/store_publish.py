"""Export local source, preview the exact store bytes, then submit a public PR."""
import asyncio
import base64
import io
import json
import re
import zipfile
from functools import partial
from datetime import datetime, timezone
from pathlib import Path

from core import store_schema, tab_folders, tab_install, integrations
from services import github_account, skills_service, skill_curator
from services.store_catalog import store_catalog, REPO

_publish_lock = asyncio.Lock()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _slug(value):
    value = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")[:64]
    if not value or not value[0].isalpha():
        value = ("item_" + value)[:64]
    return value if value not in ("routes", "services", "views") else "item_" + value


def export(kind, local_id):
    if kind not in store_schema.KINDS or not isinstance(local_id, str) or not local_id:
        raise ValueError("Choose a local store item")
    login = github_account.credentials()["login"]
    installed = next((r for r in store_catalog.installs().values() if r["kind"] == kind and r["local_id"] == local_id), None)
    if installed and installed.get("author", "").casefold() != login.casefold():
        raise ValueError("Only the original author can republish an installed store item")
    if kind == "tab":
        content = tab_install.export(local_id)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {}
            for entry in archive.infolist():
                prefix = local_id + "/"
                if not entry.filename.startswith(prefix):
                    raise ValueError("Unexpected tab archive layout")
                relative = store_schema.safe_path(entry.filename[len(prefix):])
                if relative in files or entry.file_size > store_schema.LIMITS[kind][2]:
                    raise ValueError("Duplicate or oversized tab file")
                files[relative] = archive.read(entry)
        meta = json.loads(files["tab.json"])
        name, description = meta["name"], meta["description"]
    elif kind == "skill":
        folder = Path(skills_service._skill_path(local_id)).parent
        provenance = skill_curator.describe(local_id)
        if not provenance or provenance["source"] not in (skill_curator.USER, skill_curator.IMPORTED, skill_curator.RECORDED, skill_curator.UNKNOWN):
            raise ValueError("Built-in and bundled skills cannot be shared")
        paths = tab_folders.code_files(str(folder), user=False)
        before = tab_folders.fingerprint_files(paths)[0]
        count, total_cap, single_cap = store_schema.LIMITS[kind]
        if len(paths) > count:
            raise ValueError("Skill exceeds file count limit")
        files, total = {}, 0
        for relative, filename in paths:
            store_schema.safe_path(relative)
            with open(filename, "rb") as handle:
                content = handle.read(single_cap + 1)
            total += len(content)
            if len(content) > single_cap or total > total_cap:
                raise ValueError(f"Skill exceeds size limit: {relative}")
            files[relative] = content
        if before != tab_folders.fingerprint(str(folder), user=False)[0]:
            raise ValueError("Skill changed during export. Try again.")
        skill = skills_service.get_skill(local_id) or {}
        name, description = local_id.replace("-", " ").replace("_", " ").title(), skill.get("description", "")
    elif kind == "tool":
        server = integrations.get_integration(local_id)
        if not server or server.get("kind") != "mcp_server":
            raise ValueError("Choose an MCP server")
        if server.get("mcp_type") != "http":
            raise ValueError("Local command (stdio) servers cannot be shared. Choose an HTTP MCP server.")
        name, description = server["name"], "HTTP MCP server: " + server["name"]
        # Explicit field selection prevents credentials, headers and OAuth state leaking.
        files = {"server.json": _json({"name": name, "url": server["url"], "transport": "http",
                    "auth_type": "oauth" if server.get("auth") == "oauth" else "none"})}
    else:
        from services.task_service import task_service
        task = task_service.get_task(local_id)
        if not task or task.get("builtin_action") or task.get("schedule_kind") not in ("once", "daily", "interval"):
            raise ValueError("Only your scheduled tasks can be shared, not cards or built-in routines")
        schedule_kind = task["schedule_kind"]
        field = {"once": "run_at", "daily": "run_time", "interval": "interval_seconds"}[schedule_kind]
        if schedule_kind == "once":
            stamp = datetime.fromisoformat(task["run_at"])
            if stamp.tzinfo is None:
                raise ValueError("One-time tasks need a run_at with a timezone before sharing")
            if stamp <= datetime.now(timezone.utc):
                raise ValueError("This one-time task's run_at is in the past. Schedule a future task before sharing.")
        name, description = task["name"], task["prompt"][:2000]
        template = {"title": name, "prompt": task["prompt"], "schedule": {"schedule_kind": schedule_kind, field: task[field]}}
        if task.get("model_hint"):
            template["model_hint"] = task["model_hint"]
        elif task.get("endpoint_id"):
            from core.model_endpoints import get_endpoint
            endpoint = get_endpoint(task["endpoint_id"])
            if endpoint and endpoint.get("model"):
                template["model_hint"] = endpoint["model"]
        files = {"automation.json": _json(template)}
        # Export itself must refuse invalid schedules, without converting them.
        check = store_schema.manifest({"kind": "automation", "slug": "automation", "name": name,
            "description": name, "author": login, "version": "1.0.0", "license": "MIT", "files": ["automation.json"]})
        store_schema.validate_bytes(check, {**files, "manifest.json": _json(check)})
    return {"files": files, "slug": installed["slug"] if installed else _slug(local_id if kind in ("tab", "skill") else name),
            "name": name, "description": description or name}


async def suggestions(kind, local_id):
    login = github_account.credentials()["login"]
    source = await asyncio.to_thread(export, kind, local_id)
    if github_account.credentials()["login"] != login:
        raise ValueError("The GitHub account changed. Preview and publish again.")
    snapshot = await store_catalog.snapshot(refresh=True)
    old = next((i for i in snapshot["index"]["items"] if i["kind"] == kind and i["slug"] == source["slug"] and i["author"].casefold() == login.casefold()), None)
    version = "1.0.0"
    if old:
        major, minor, patch = old["version"].split("+", 1)[0].split("-", 1)[0].split(".")
        version = f"{major}.{minor}.{int(patch) + 1}"
    return {key: source[key] for key in ("slug", "name", "description")} | {"version": version}


async def prepare(kind, local_id, slug=None, name=None, description=None, version=None):
    login = github_account.credentials()["login"]
    source = await asyncio.to_thread(export, kind, local_id)
    if github_account.credentials()["login"] != login:
        raise ValueError("The GitHub account changed. Preview and publish again.")
    slug = source["slug"] if slug is None else slug
    # Publishing must not mistake a saved/offline catalog for live ownership.
    snapshot = await store_catalog.snapshot(refresh=True)
    if snapshot.get("stale"):
        raise ValueError("The live store catalog is unavailable. Refresh it before publishing.")
    old = next((i for i in snapshot["index"]["items"] if i["kind"] == kind and i["slug"] == slug), None)
    if old and old["author"].casefold() != login.casefold():
        raise ValueError("This slug belongs to another author. Choose a different slug.")
    if version is None:
        major, minor, patch = old["version"].split("+", 1)[0].split("-", 1)[0].split(".") if old else ("1", "0", "-1")
        version = f"{major}.{minor}.{int(patch) + 1}"
    meta = store_schema.manifest({"kind": kind, "slug": slug, "name": source["name"] if name is None else name,
        "description": source["description"] if description is None else description, "author": login,
        "version": version, "license": "MIT", "files": sorted(source["files"])})
    if old and store_schema.semver_key(version) <= store_schema.semver_key(old["version"]):
        raise ValueError("An update needs a higher semver version than the catalog version")
    files = source["files"]
    if kind == "tab":
        tab = json.loads(files["tab.json"])
        tab.update(slug=slug, name=meta["name"], description=meta["description"], version=version)
        files["tab.json"] = _json(tab)
    files["manifest.json"] = _json(meta)
    store_schema.validate_bytes(meta, files)
    return {"manifest": meta, "files": {f"items/{kind}/{slug}/{p}": content.decode("utf-8") for p, content in sorted(files.items())},
        "preview_hash": store_schema.archive_sha256(files), "update": bool(old),
        "removed_files": [f"items/{kind}/{slug}/{p}" for p in sorted(set(old["files"]) - set(meta["files"]))] if old else []}


def _pr(result):
    url = result.get("html_url", "")
    if not re.fullmatch(r"https://github\.com/david-darr/kairos-store/pull/[1-9][0-9]*", url) or type(result.get("number")) is not int:
        raise ValueError("GitHub returned an invalid pull request link")
    return {"url": url, "number": result["number"]}


async def _existing(head, api):
    prs = await api("GET", f"/repos/{REPO}/pulls", params={"state": "open", "head": head, "base": "main", "per_page": 100})
    return _pr(prs[0]) if prs else None


async def publish(kind, local_id, slug, name, description, version, preview_hash, confirmed_public=False):
    if confirmed_public is not True:
        raise ValueError("Confirm that this will be public on GitHub under the MIT licence")
    async with _publish_lock:
        preview = await prepare(kind, local_id, slug, name, description, version)
        if preview_hash != preview["preview_hash"]:
            raise ValueError("The files changed since preview. Preview them again before publishing.")
        login = preview["manifest"]["author"]
        api = partial(github_account.api, expected_login=login)
        upstream = f"/repos/{REPO}"
        branch = f"kairos/{kind}-{slug}-{version}"
        # GitHub refuses to fork a repository into its own owner's account,
        # so the store's owner branches on the store itself.
        owner = login.casefold() == REPO.split("/")[0].casefold()
        fork = upstream if owner else f"/repos/{login}/kairos-store"
        head = branch if owner else f"{login}:{branch}"
        if not owner:
            await api("POST", upstream + "/forks", body={"default_branch_only": True})
            for attempt in range(10):
                try:
                    info = await api("GET", fork)
                    if not info.get("fork") or (info.get("parent") or {}).get("full_name", "").casefold() != REPO.casefold():
                        raise ValueError("Your kairos-store repository is not a fork of david-darr/kairos-store")
                    break
                except github_account.GitHubError as exc:
                    if exc.status != 404:
                        raise
                    if attempt == 9:
                        raise ValueError("GitHub is still creating your fork. Try publishing again shortly.") from None
                    await asyncio.sleep(2)
            await api("POST", fork + "/merge-upstream", body={"branch": "main"})
        main = await api("GET", upstream + "/commits/main")
        tree = []
        for path, content in preview["files"].items():
            blob = await api("POST", fork + "/git/blobs", body={"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"})
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree.extend({"path": path, "mode": "100644", "type": "blob", "sha": None} for path in preview["removed_files"])
        built = await api("POST", fork + "/git/trees", body={"base_tree": main["commit"]["tree"]["sha"], "tree": tree})
        title = f"{'Update' if preview['update'] else 'Add'} {kind}: {name} {version}"
        commit = await api("POST", fork + "/git/commits", body={"message": title, "tree": built["sha"], "parents": [main["sha"]]})
        try:
            await api("POST", fork + "/git/refs", body={"ref": "refs/heads/" + branch, "sha": commit["sha"]})
        except github_account.GitHubError as exc:
            if exc.status != 422:
                raise
            existing = await _existing(f"{login}:{branch}", api)
            if existing:
                return existing
            # A previous attempt may have committed successfully before PR creation failed.
            ref = await api("GET", fork + "/git/ref/heads/" + branch)
            previous = await api("GET", fork + "/git/commits/" + ref["object"]["sha"])
            if previous["tree"]["sha"] != built["sha"]:
                raise ValueError("This submission branch already contains different files. Choose a new version.") from None
        body = "Submitted from Kairos and offered under the MIT licence.\n\nFiles:\n" + "\n".join("- `" + p + "`" for p in preview["files"])
        if preview["removed_files"]:
            body += "\n\nRemoved files:\n" + "\n".join("- `" + p + "`" for p in preview["removed_files"])
        try:
            result = await api("POST", upstream + "/pulls", body={"head": head, "base": "main", "title": title, "body": body})
            return _pr(result)
        except github_account.GitHubError as exc:
            if exc.status == 422:
                existing = await _existing(f"{login}:{branch}", api)
                if existing:
                    return existing
            raise


async def submissions():
    login = github_account.credentials()["login"]
    result = []
    for page in range(1, 11):
        prs = await github_account.api("GET", f"/repos/{REPO}/pulls", expected_login=login, params={"state": "all", "per_page": 100, "page": page, "sort": "updated", "direction": "desc"})
        for pr in prs:
            if (pr.get("user") or {}).get("login", "").casefold() == login.casefold():
                result.append({**_pr(pr), "title": pr["title"], "state": "merged" if pr.get("merged_at") else pr["state"], "updated_at": pr["updated_at"]})
        if len(prs) < 100:
            break
    return result
