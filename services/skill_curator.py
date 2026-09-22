"""Skill curation: where each skill came from, whether it was scanned, and
whether a model may use it.

The scanner is Hermes Agent's (services/skills_guard.py); this module is
JARVIS's policy around it, following Hermes's trust table:

  - bundled (ships with the app) and user (written in the app) are trusted
    and not scanned, as Hermes treats its own "builtin" skills;
  - imported (from a file) is a community source: a "dangerous" verdict is
    refused outright, a "caution" verdict needs the user's explicit OK;
  - a skill with no record (created before curation existed and not a
    bundled one) is treated as imported and scanned once.

A skill whose current content scans "dangerous" is hidden from every model
until an admin approves that exact content. Approval is bound to the content
hash, so editing the skill afterwards needs approving again.

Provenance is kept in SKILLS_DIR/.provenance.json, never in the SKILL.md
itself: frontmatter is written by whoever wrote the skill, so a skill could
otherwise declare its own origin. A SKILL.md never grants a tool permission
either - nothing reads `allowed-tools`, and a test holds that line.
"""
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from services import skill_linter, skills_guard, skills_service

logger = logging.getLogger(__name__)

BUNDLED, USER, IMPORTED, UNKNOWN = "bundled", "user", "imported", "unknown"
_SCANNED_SOURCES = (IMPORTED, UNKNOWN)
# Hermes's source ids: "official" resolves to its builtin trust level, anything
# else that is not a trusted repo resolves to "community".
_GUARD_SOURCE = {IMPORTED: "community", UNKNOWN: "community"}

_LOCK = threading.RLock()


class SkillImportRefused(Exception):
    """An import the scan did not allow. ``needs_confirmation`` is True when
    the user may still choose to import it (a "caution" verdict), False when
    nothing overrides it (a "dangerous" verdict)."""

    def __init__(self, report: str, needs_confirmation: bool, findings: list):
        super().__init__(report)
        self.report = report
        self.needs_confirmation = needs_confirmation
        self.findings = findings


def _provenance_path() -> str:
    # Read through skills_service each time so a relocated SKILLS_DIR (tests,
    # a changed data directory) is followed rather than captured at import.
    return os.path.join(skills_service.SKILLS_DIR, ".provenance.json")


def _load() -> dict:
    data = read_json(_provenance_path(), {})
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    os.makedirs(skills_service.SKILLS_DIR, exist_ok=True)
    write_json_atomic(_provenance_path(), data)


def _skill_dir(slug: str) -> Path:
    return Path(skills_service._skill_path(slug)).parent


def _content_hash(slug: str) -> Optional[str]:
    skill_dir = _skill_dir(slug)
    if not (skill_dir / "SKILL.md").exists():
        return None
    return skills_guard.content_hash(skill_dir)


def _scan_dir(skill_dir: Path, source: str) -> dict:
    result = skills_guard.scan_skill(skill_dir, source=_GUARD_SOURCE.get(source, "community"))
    return {
        "verdict": result.verdict,
        "summary": result.summary,
        "report": skills_guard.format_scan_report(result),
        "findings": [
            {"severity": f.severity, "category": f.category, "pattern": f.pattern_id,
             "file": f.file, "line": f.line, "match": f.match, "description": f.description}
            for f in result.findings
        ],
        "scanner_version": skills_guard.SCANNER_VERSION,
        "scanned_at": result.scanned_at,
    }


def _bundled_slugs() -> set:
    from core import settings as settings_store
    return set(settings_store.get_setting("seeded_skills") or [])


def _reconcile(data: dict, slug: str) -> Optional[dict]:
    """Bring one skill's record in line with its current content. Returns the
    record, or None if the skill no longer exists. Mutates ``data``."""
    current = _content_hash(slug)
    if current is None:
        data.pop(slug, None)
        return None
    record = data.get(slug)
    if record is None:
        record = {"source": BUNDLED if slug in _bundled_slugs() else UNKNOWN,
                  "origin": None, "recorded_at": time.time()}
        data[slug] = record
    if record.get("content_hash") != current or (
            record["source"] in _SCANNED_SOURCES
            and (record.get("scan") or {}).get("scanner_version") != skills_guard.SCANNER_VERSION):
        record["content_hash"] = current
        record["scan"] = _scan_dir(_skill_dir(slug), record["source"]) \
            if record["source"] in _SCANNED_SOURCES else None
    return record


def _blocked(record: dict) -> bool:
    scan = record.get("scan") or {}
    return scan.get("verdict") == "dangerous" and record.get("approved_hash") != record.get("content_hash")


def record(slug: str, source: str, origin: Optional[str] = None) -> dict:
    """Note where a skill came from, at the moment it is created or imported.
    Re-recording (re-importing over an existing skill) replaces the origin."""
    with _LOCK:
        data = _load()
        data[slug] = {"source": source, "origin": origin, "recorded_at": time.time()}
        rec = _reconcile(data, slug)
        _save(data)
        return rec


def forget(slug: str) -> None:
    with _LOCK:
        data = _load()
        if data.pop(slug, None) is not None:
            _save(data)


def approve(slug: str) -> dict:
    """Allow models to use the skill's current content despite its scan."""
    with _LOCK:
        data = _load()
        rec = _reconcile(data, slug)
        if rec is None:
            raise FileNotFoundError(f"no such skill: {slug}")
        rec["approved_hash"] = rec["content_hash"]
        rec["approved_at"] = time.time()
        _save(data)
        return rec


def describe(slug: str) -> Optional[dict]:
    """Provenance, scan and lint for one skill, as the Brain tab shows it."""
    with _LOCK:
        data = _load()
        before = repr(data.get(slug))
        rec = _reconcile(data, slug)
        if repr(data.get(slug)) != before:
            _save(data)
    if rec is None:
        return None
    skill = skills_service.get_skill(slug) or {}
    scan = rec.get("scan")
    return {
        "source": rec["source"],
        "origin": rec.get("origin"),
        "scan": None if scan is None else {k: scan[k] for k in ("verdict", "summary", "findings", "scanner_version", "scanned_at")},
        "blocked_for_models": _blocked(rec),
        "approved": bool(rec.get("approved_hash")) and rec.get("approved_hash") == rec.get("content_hash"),
        "lint": [f.to_dict() for f in skill_linter.lint(skill.get("description", ""), skill.get("body", ""), _skill_dir(slug))],
    }


def model_visible(slug: str) -> bool:
    """Whether any model may list or read this skill."""
    with _LOCK:
        data = _load()
        before = repr(data.get(slug))
        rec = _reconcile(data, slug)
        if repr(data.get(slug)) != before:
            _save(data)
    return rec is not None and not _blocked(rec)


def check_import(slug: str, skill_md: str, confirmed: bool = False) -> dict:
    """Scan a SKILL.md before it is written. Raises SkillImportRefused when
    Hermes's install policy for a community source does not allow it (a
    confirmed "caution" is allowed; "dangerous" never is). Returns the scan."""
    with tempfile.TemporaryDirectory(prefix="jarvis-skill-import-") as tmp:
        staged = Path(tmp) / slug
        staged.mkdir()
        (staged / "SKILL.md").write_text(skill_md, encoding="utf-8")
        result = skills_guard.scan_skill(staged, source="community")
    allowed, reason = skills_guard.should_allow_install(result, force=confirmed)
    report = skills_guard.format_scan_report(result)
    findings = [{"severity": f.severity, "pattern": f.pattern_id, "line": f.line,
                 "match": f.match, "description": f.description} for f in result.findings]
    if not allowed:
        # Hermes's hard block: force never overrides "dangerous" from a community source.
        needs_confirmation = result.verdict != "dangerous"
        raise SkillImportRefused(report, needs_confirmation, findings)
    return {"verdict": result.verdict, "report": report, "reason": reason}
