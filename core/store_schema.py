"""Store v1 wire contract. Mirrored in kairos-store/tools/store_schema.py.

Only stdlib dependencies; CI and the installer validate the same bytes.
"""
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit

KINDS = {"tab", "skill", "tool", "automation"}
SLUG = re.compile(r"[a-z][a-z0-9_]{0,63}")
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", re.ASCII)
TEXT_TYPES = {".md", ".txt", ".py", ".sh", ".bash", ".js", ".ts", ".rb", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini", ".conf", ".html", ".css", ".xml", ".tex", ".r", ".jl", ".pl", ".php"}
LIMITS = {"tab": (500, 20 * 1024 * 1024, 20 * 1024 * 1024),
          "skill": (50, 5 * 1024 * 1024, 256 * 1024),
          "tool": (1, 100_000, 100_000), "automation": (1, 100_000, 100_000)}
MAX_INDEX_BYTES = 2 * 1024 * 1024
SECRETS = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})|(?:api[_-]?key|token|secret|password)[\"']?\s*[=:]\s*[\"'][^\"'\s]{20,}")


def safe_path(value):
    if not isinstance(value, str) or len(value) > 240 or not value.isascii():
        raise ValueError("Invalid file path")
    parts = value.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in ("", ".", "..")
           or p.endswith((".", " ")) or p.lower().startswith(".env")
           or p in ("__pycache__", ".git")
           or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.I) for p in parts):
        raise ValueError(f"Unsafe file path: {value}")
    return value


def https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid HTTPS URL")
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment or u.query or u.port not in (None, 443) or any(c.isspace() for c in value):
        raise ValueError("URLs must be HTTPS without credentials, query strings or fragments")
    return value


def manifest(value, kind=None, slug=None):
    required = {"kind", "slug", "name", "description", "author", "version", "license", "files"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"min_kairos_version"}:
        raise ValueError("Invalid manifest fields")
    if not isinstance(value["kind"], str) or value["kind"] not in KINDS or (kind is not None and value["kind"] != kind):
        raise ValueError("Wrong kind")
    if not isinstance(value["slug"], str) or not SLUG.fullmatch(value["slug"]) or value["slug"] in ("routes", "services", "views") or (slug is not None and value["slug"] != slug):
        raise ValueError("Invalid slug or folder mismatch")
    safe_path(value["slug"])
    for key, cap in (("name", 120), ("description", 2000), ("author", 39), ("version", 100)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > cap:
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", value["author"]):
        raise ValueError("author must be a GitHub login")
    if value["license"] != "MIT" or not SEMVER.fullmatch(value["version"]):
        raise ValueError("MIT license and semver version required")
    if "min_kairos_version" in value and (not isinstance(value["min_kairos_version"], str) or not SEMVER.fullmatch(value["min_kairos_version"])):
        raise ValueError("Invalid min_kairos_version")
    files = value["files"]
    count = LIMITS[value["kind"]][0]
    if not isinstance(files, list) or not 1 <= len(files) <= count:
        raise ValueError("Invalid files count")
    for f in files:
        safe_path(f)
    if len({f.casefold() for f in files}) != len(files) or "manifest.json" in files:
        raise ValueError("Duplicate or reserved file")
    for f in files:
        ignore_file = value["kind"] == "skill" and f in (".skillignore", ".clawhubignore")
        if PurePosixPath(f).suffix.lower() not in TEXT_TYPES and not ignore_file:
            raise ValueError(f"File type not allowed: {f}")
    required_files = {"tab": {"tab.json", "routes.py"}, "skill": {"SKILL.md"}, "tool": {"server.json"}, "automation": {"automation.json"}}[value["kind"]]
    if not required_files <= set(files) or (value["kind"] in ("tool", "automation") and set(files) != required_files):
        raise ValueError("Missing or unexpected payload files")
    return value


def archive_sha256(files):
    """SHA256: sorted ASCII paths; uint64be path length, path, uint64be size, bytes."""
    h = hashlib.sha256()
    for path in sorted(files):
        name, content = path.encode("utf-8"), files[path]
        h.update(len(name).to_bytes(8, "big")); h.update(name)
        h.update(len(content).to_bytes(8, "big")); h.update(content)
    return h.hexdigest()


def tab_archive(slug, files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            if name != "manifest.json":
                z.writestr(slug + "/" + name, files[name])
    if len(out.getvalue()) > 5 * 1024 * 1024:
        raise ValueError("Tab exceeds 5 MB compressed")
    return out.getvalue()


def validate_bytes(meta, files):
    manifest(meta)
    if set(files) != {"manifest.json", *meta["files"]}:
        raise ValueError("Manifest files must match item exactly")
    if len({name.casefold() for name in files}) != len(files):
        raise ValueError("Case-insensitive duplicate file")
    _, total_limit, single_limit = LIMITS[meta["kind"]]
    if len(files["manifest.json"]) > 16_384 or sum(len(files[f]) for f in meta["files"]) > total_limit:
        raise ValueError("Item exceeds size limit")
    for name, content in files.items():
        safe_path(name)
        if len(content) > single_limit or (name == "SKILL.md" and len(content) > 100_000):
            raise ValueError(f"File exceeds size limit: {name}")
        text = content.decode("utf-8")
        if any(ord(c) < 32 and c not in "\r\n\t\f" for c in text):
            raise ValueError(f"Non-text file: {name}")
        if SECRETS.search(text):
            raise ValueError(f"Possible secret in {name}")
        if re.search(r"\bhttp://", text, re.I):
            raise ValueError(f"Only HTTPS URLs allowed: {name}")
    kind = meta["kind"]
    if kind == "tool":
        server = json.loads(files["server.json"])
        if not isinstance(server, dict) or set(server) != {"name", "url", "transport", "auth_type"}:
            raise ValueError("server.json accepts name, url, transport, auth_type only; no secrets or headers")
        if not isinstance(server["name"], str) or not server["name"].strip() or len(server["name"]) > 120 or server["transport"] != "http" or server["auth_type"] not in ("none", "oauth"):
            raise ValueError("Unsupported server transport or auth_type")
        https_url(server["url"])
    elif kind == "automation":
        task = json.loads(files["automation.json"])
        if not isinstance(task, dict) or not {"title", "prompt", "schedule"} <= task.keys() or task.keys() - {"title", "prompt", "schedule", "model_hint"}:
            raise ValueError("Invalid automation fields; no ids or delivery channel")
        for key in ("title", "prompt", *(["model_hint"] if "model_hint" in task else [])):
            if not isinstance(task[key], str) or not task[key].strip():
                raise ValueError(f"Invalid automation {key}")
        schedule = task["schedule"]
        if not isinstance(schedule, dict):
            raise ValueError("schedule must be an object")
        schedule_kind = schedule.get("schedule_kind")
        if not isinstance(schedule_kind, str):
            raise ValueError("Invalid schedule_kind")
        field = {"once": "run_at", "interval": "interval_seconds", "daily": "run_time"}.get(schedule_kind)
        if not field or set(schedule) != {"schedule_kind", field}:
            raise ValueError("Invalid schedule fields")
        value = schedule[field]
        if field == "run_at":
            if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError("run_at must be an ISO datetime with timezone")
        elif field == "interval_seconds":
            if type(value) is not int or not 1 <= value <= 31536000:
                raise ValueError("interval_seconds must be 1..31536000")
        elif not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("run_time must be HH:MM in local time")
    elif kind == "tab":
        tab = json.loads(files["tab.json"])
        if not isinstance(tab, dict) or tab.get("slug") != meta["slug"]:
            raise ValueError("tab.json slug must match folder")
        for key in ("name", "version", "description"):
            if not isinstance(tab.get(key), str) or (key != "description" and not tab[key].strip()):
                raise ValueError(f"Invalid tab {key}")
        if type(tab.get("api")) is not int or tab["api"] < 1 or not isinstance(tab.get("hooks"), list) or any(not isinstance(h, str) for h in tab["hooks"]):
            raise ValueError("Tab needs positive api and hooks list")
        for key in ("icon_svg", "blurb", "detail", "reads"):
            if key in tab and not isinstance(tab[key], str):
                raise ValueError(f"Invalid tab {key}")
        tab_archive(meta["slug"], files)
    return meta


def revocations(value):
    if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list) or len(value["items"]) > 5000:
        raise ValueError("Invalid revoked.json")
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"kind", "slug", "versions", "reason"} or not isinstance(item["kind"], str) or item["kind"] not in KINDS or not isinstance(item["slug"], str) or not SLUG.fullmatch(item["slug"]):
            raise ValueError("Invalid revoked item")
        v = item["versions"]
        if v != "all" and (not isinstance(v, list) or not v or any(not isinstance(s, str) or not SEMVER.fullmatch(s) for s in v)):
            raise ValueError("versions must be 'all' or a nonempty semver list")
        if not isinstance(item["reason"], str) or not 1 <= len(item["reason"].strip()) <= 2000:
            raise ValueError("Revocation reason required")
    return value


def semver_key(value):
    """Semver precedence, ignoring build metadata. All inputs were validated."""
    core, _, pre = value.split("+", 1)[0].partition("-")
    release = tuple(int(n) for n in core.split("."))
    identifiers = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))
    return release, (0, identifiers) if pre else (1, ())
