"""Skills: disk-backed SKILL.md files (frontmatter + body), managed in Tool Store.
Matches the pattern researched from Odysseus (specs/memory-skills.md)
— portable, user-editable procedure files, not a bespoke DB table.

Public GitHub SKILL.md imports use this service's community scan and
provenance path. Auto-extraction from conversations remains out of scope.

A skill that cannot be read (not UTF-8, an unreadable file) fails closed
(roadmap phase 6, 2026-10-06): list_skills() gives it an `error` and no
content, and get_skill() refuses it, so no model is offered it, and the rest
still work. Before this, one such file made list_skills() raise, which took
every skill away from every model and emptied Tool Store's list.
"""
import hashlib
import logging
import os
import re
import shutil
from typing import Optional

from core.constants import BASE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

SKILLS_DIR = os.path.join(DATA_DIR, "skills")

# Skills that ship with the app (David's ask 2026-09-03). They live here
# rather than in data/skills because data/ is deliberately excluded from
# packaged builds (it holds credentials and chat history), so anything
# seeded there in development would never reach a real download.
SKILL_TEMPLATES_DIR = os.path.join(BASE_DIR, "skill_templates")

# Before seed hashes were tracked, this exact bundled text was shipped.
# Normalize line endings only: even a small user edit must prevent refresh.
_LEGACY_SEED_HASHES = {
    "build-custom-tab": {"c12313f03035060c100ceac976653c6c1ccbe8f759285a44925b700009141cb2"},
}


def _seed_hash(path: str) -> str:
    return hashlib.sha256(_read(path).encode("utf-8")).hexdigest()

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
# Hermes's skill-name rule. A slug becomes a directory name, so it must never
# carry a separator or a dot: "..\\..\\x" used to reach a SKILL.md outside the
# skills folder through the API and the model's read_skill tool.
_VALID_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _slugify(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-")
    slug = _SLUG_RE.sub("", slug).lstrip("-")
    if not slug:
        raise ValueError("skill name produces an empty slug")
    return slug


def _skill_path(slug: str) -> str:
    """The one place a slug becomes a path, so the one place it is checked."""
    if not isinstance(slug, str) or not _VALID_SLUG_RE.fullmatch(slug):
        raise ValueError(f"invalid skill name: {slug!r}")
    return os.path.join(SKILLS_DIR, slug, "SKILL.md")


class SkillUnreadable(ValueError):
    """A SKILL.md that cannot be read as text."""


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise SkillUnreadable("its SKILL.md is not UTF-8 text")
    except OSError as e:
        raise SkillUnreadable(f"its SKILL.md could not be opened ({e.strerror or e})")


def _frontmatter_value(frontmatter: str, key: str) -> str:
    for line in (frontmatter or "").splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _parse(raw: str) -> dict:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {"description": "", "body": raw.strip()}
    frontmatter_text, body = match.groups()
    description = ""
    lines = frontmatter_text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        # YAML block scalar ("description: |" / ">") — the real text is the
        # indented lines that follow, not the marker. The bundled humanizer
        # skill uses this form, and without handling it the skill manager
        # showed its description as a literal "|".
        if value in ("|", ">", "|-", ">-"):
            collected = []
            for follow in lines[i + 1:]:
                if follow.strip() and not follow.startswith((" ", "\t")):
                    break  # dedented — next frontmatter key
                collected.append(follow.strip())
            value = " ".join(p for p in collected if p)
        description = value
        break
    return {"description": description, "body": body.strip(), "frontmatter": frontmatter_text}


def _render(description: str, body: str, frontmatter: str = "") -> str:
    """Rebuild a SKILL.md, keeping any frontmatter keys the app doesn't model.

    This used to emit only `description`, which silently destroyed everything
    else the moment a skill was saved or re-imported — `name`, `license`,
    `version`, `allowed-tools` and anything else. Found live on the bundled
    humanizer skill, whose MIT `license:` line disappeared after an edit.
    The app understands one key; it has no business deleting the rest.
    """
    other = []
    if frontmatter:
        skipping_block = False
        for line in frontmatter.splitlines():
            is_continuation = line.startswith((" ", "\t")) or not line.strip()
            if skipping_block and is_continuation:
                continue  # a block-scalar description's indented lines
            skipping_block = False
            if line.startswith("description:"):
                # Dropped here and re-emitted below with the new value.
                if line.split(":", 1)[1].strip() in ("|", ">", "|-", ">-"):
                    skipping_block = True
                continue
            other.append(line)

    lines = [f"description: {description}"] + other
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body.strip() + "\n"


def seed_default_skills() -> list[str]:
    """Copy the bundled skills into the user's skills folder on first run.

    Tracked per-slug in settings rather than "copy if the folder is missing":
    a user who deletes a bundled skill means it, and resurrecting it on every
    launch would be the app arguing with them. Each slug is therefore only
    ever seeded once. Refresh SKILL.md only while it still matches the last
    seed (or a known old bundled copy); never replace edits or resurrect deletions.

    Returns the slugs actually seeded this call.
    """
    from core import settings as settings_store

    if not os.path.isdir(SKILL_TEMPLATES_DIR):
        return []

    already = set(settings_store.get_setting("seeded_skills") or [])
    hashes = dict(settings_store.get_setting("seeded_skill_hashes") or {})
    old_hashes = hashes.copy()
    seeded = []
    for slug in sorted(os.listdir(SKILL_TEMPLATES_DIR)):
        template_dir = os.path.join(SKILL_TEMPLATES_DIR, slug)
        if not os.path.isdir(template_dir):
            continue
        target_dir = os.path.join(SKILLS_DIR, slug)
        try:
            src_skill = os.path.join(template_dir, "SKILL.md")
            dst_skill = os.path.join(target_dir, "SKILL.md")
            template_hash = _seed_hash(src_skill)
            if slug in already:
                if not os.path.isfile(dst_skill):
                    continue  # A deletion is deliberate.
                current_hash = _seed_hash(dst_skill)
                known = _LEGACY_SEED_HASHES.get(slug, set()) | {hashes.get(slug), template_hash}
                if current_hash in known:
                    if current_hash != template_hash:
                        shutil.copy2(src_skill, dst_skill)
                        logger.info("skills_service: refreshed bundled skill '%s'", slug)
                    hashes[slug] = template_hash
                continue
            os.makedirs(target_dir, exist_ok=True)
            for filename in os.listdir(template_dir):
                src = os.path.join(template_dir, filename)
                dst = os.path.join(target_dir, filename)
                # Never clobber a skill the user already has under this name.
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
            seeded.append(slug)
            if _seed_hash(dst_skill) == template_hash:
                hashes[slug] = template_hash
            from services import skill_curator
            skill_curator.record(slug, skill_curator.BUNDLED)
        except (OSError, SkillUnreadable):
            logger.exception("skills_service: couldn't seed bundled skill '%s'", slug)

    if seeded or hashes != old_hashes:
        settings_store.update_settings(seeded_skills=sorted(already | set(seeded)), seeded_skill_hashes=hashes)
    if seeded:
        logger.info("skills_service: seeded bundled skills %s", ", ".join(seeded))
    return seeded


def list_skills() -> list[dict]:
    if not os.path.isdir(SKILLS_DIR):
        return []
    skills = []
    for slug in sorted(os.listdir(SKILLS_DIR)):
        # A folder the app could not have created (dotfiles, hand-made names)
        # is skipped rather than failing the whole listing.
        if not _VALID_SLUG_RE.fullmatch(slug):
            continue
        path = _skill_path(slug)
        if os.path.exists(path):
            try:
                parsed = _parse(_read(path))
            except SkillUnreadable as e:
                skills.append({"slug": slug, "description": "", "error": f"Can't be read: {e}."})
                continue
            skills.append({"slug": slug, "description": parsed["description"],
                           "version": _frontmatter_value(parsed.get("frontmatter", ""), "version") or None})
    return skills


def get_skill(slug: str) -> Optional[dict]:
    path = _skill_path(slug)
    if not os.path.exists(path):
        return None
    return {"slug": slug, **_parse(_read(path))}


def create_skill(name: str, description: str, body: str) -> dict:
    slug = _slugify(name)
    path = _skill_path(slug)
    if os.path.exists(path):
        raise FileExistsError(f"skill '{slug}' already exists")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(description, body))
    from services import skill_curator
    skill_curator.record(slug, skill_curator.USER)
    return {"slug": slug, "description": description, "body": body}


def update_skill(slug: str, description: str, body: str) -> dict:
    path = _skill_path(slug)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such skill: {slug}")
    # Read the existing frontmatter first so keys the app doesn't model
    # (name, license, version, ...) survive the write. An unreadable file has
    # none worth keeping: saving over it is how it gets fixed.
    try:
        existing = _parse(_read(path))
    except SkillUnreadable:
        existing = {}
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(description, body, existing.get("frontmatter", "")))
    return {"slug": slug, "description": description, "body": body}


def import_skill(filename: str, raw_content: str, confirmed: bool = False, *,
                 name: str | None = None, origin: str | None = None,
                 replace: bool = True, source: str = "imported", supporting_files=None) -> dict:
    """Import local file text or a fetched community SKILL.md.

    Local imports may replace an existing skill; URL installs set replace=False
    so an online source cannot silently overwrite a user's skill.
    If the file already has SKILL.md-shaped frontmatter, its description is
    used; otherwise the whole file becomes the body with no description.

    An imported file is untrusted: it is scanned before anything is written
    (services/skill_curator.py), and skill_curator.SkillImportRefused is
    raised if the scan does not allow it. ``confirmed`` is the user's
    explicit "import anyway" after seeing a caution report; it never
    overrides a dangerous verdict.
    """
    from services import skill_curator
    if source not in (skill_curator.IMPORTED, skill_curator.RECORDED):
        raise ValueError("imports must have an untrusted source")
    name = name or os.path.splitext(os.path.basename(filename))[0]
    parsed = _parse(raw_content)
    slug = _slugify(name)
    path = _skill_path(slug)
    rendered = _render(parsed["description"], parsed["body"], parsed.get("frontmatter", ""))
    scan = skill_curator.check_import(slug, rendered, confirmed=confirmed, supporting_files=supporting_files)
    if supporting_files is not None:
        import uuid
        from pathlib import Path
        from core import tab_folders
        from core.store_schema import safe_path
        destination = Path(path).parent
        if tab_folders.is_link(SKILLS_DIR) or tab_folders.is_link(str(destination)):
            raise ValueError("Skill destination cannot be a link")
        if destination.exists():
            tab_folders.code_files(str(destination), user=False)
            if not replace:
                raise FileExistsError(f"skill '{slug}' already exists")
        os.makedirs(SKILLS_DIR, exist_ok=True)
        staged = Path(SKILLS_DIR) / (".install-" + uuid.uuid4().hex)
        backup = Path(SKILLS_DIR) / (".previous-" + uuid.uuid4().hex)
        staged.mkdir()
        try:
            (staged / "SKILL.md").write_text(rendered, encoding="utf-8")
            for relative, content in supporting_files.items():
                safe_path(relative)
                target = staged.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            if destination.exists():
                os.rename(destination, backup)
            try:
                os.rename(staged, destination)
            except OSError:
                if backup.exists():
                    os.rename(backup, destination)
                raise
        finally:
            for folder in (staged, backup):
                if folder.exists():
                    shutil.rmtree(folder)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w" if replace else "x", encoding="utf-8") as f:
            f.write(rendered)
    skill_curator.record(slug, source, origin=origin or os.path.basename(filename))
    return {"slug": slug, "description": parsed["description"], "body": parsed["body"],
            "scan": scan["verdict"]}


def delete_skill(slug: str) -> None:
    path = _skill_path(slug)
    if os.path.exists(path):
        from core import tab_folders
        # Bundle files share the same checked folder as SKILL.md.
        tab_folders.code_files(os.path.dirname(path), user=False)
        skill_dir = os.path.dirname(path)
        shutil.rmtree(skill_dir)
        from services import skill_curator
        skill_curator.forget(slug)
