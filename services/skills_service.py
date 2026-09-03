"""Skills: disk-backed SKILL.md files (frontmatter + body), the Brain tab's
skills half. Matches the pattern researched from Odysseus (specs/memory-skills.md)
— portable, user-editable procedure files, not a bespoke DB table.

Auto-extraction from conversations and import-from-URL (both real Odysseus
features) are deliberately out of this pass — this is the CRUD foundation
those would build on top of, not a commitment to build them yet.
"""
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

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    slug = name.strip().lower().replace(" ", "-")
    slug = _SLUG_RE.sub("", slug)
    if not slug:
        raise ValueError("skill name produces an empty slug")
    return slug


def _skill_path(slug: str) -> str:
    return os.path.join(SKILLS_DIR, slug, "SKILL.md")


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
        # skill uses this form, and without handling it the Brain tab showed
        # its description as a literal "|".
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
    ever seeded once.

    Returns the slugs actually seeded this call.
    """
    from core import settings as settings_store

    if not os.path.isdir(SKILL_TEMPLATES_DIR):
        return []

    already = set(settings_store.get_setting("seeded_skills") or [])
    seeded = []
    for slug in sorted(os.listdir(SKILL_TEMPLATES_DIR)):
        template_dir = os.path.join(SKILL_TEMPLATES_DIR, slug)
        if not os.path.isdir(template_dir) or slug in already:
            continue
        target_dir = os.path.join(SKILLS_DIR, slug)
        try:
            os.makedirs(target_dir, exist_ok=True)
            for filename in os.listdir(template_dir):
                src = os.path.join(template_dir, filename)
                dst = os.path.join(target_dir, filename)
                # Never clobber a skill the user already has under this name.
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
            seeded.append(slug)
        except OSError:
            logger.exception("skills_service: couldn't seed bundled skill '%s'", slug)

    if seeded:
        settings_store.update_settings(seeded_skills=sorted(already | set(seeded)))
        logger.info("skills_service: seeded bundled skills %s", ", ".join(seeded))
    return seeded


def list_skills() -> list[dict]:
    if not os.path.isdir(SKILLS_DIR):
        return []
    skills = []
    for slug in sorted(os.listdir(SKILLS_DIR)):
        path = _skill_path(slug)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                parsed = _parse(f.read())
            skills.append({"slug": slug, "description": parsed["description"]})
    return skills


def get_skill(slug: str) -> Optional[dict]:
    path = _skill_path(slug)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        parsed = _parse(f.read())
    return {"slug": slug, **parsed}


def create_skill(name: str, description: str, body: str) -> dict:
    slug = _slugify(name)
    path = _skill_path(slug)
    if os.path.exists(path):
        raise FileExistsError(f"skill '{slug}' already exists")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(description, body))
    return {"slug": slug, "description": description, "body": body}


def update_skill(slug: str, description: str, body: str) -> dict:
    path = _skill_path(slug)
    if not os.path.exists(path):
        raise FileNotFoundError(f"no such skill: {slug}")
    # Read the existing frontmatter first so keys the app doesn't model
    # (name, license, version, ...) survive the write.
    with open(path, "r", encoding="utf-8") as f:
        existing = _parse(f.read())
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(description, body, existing.get("frontmatter", "")))
    return {"slug": slug, "description": description, "body": body}


def import_skill(filename: str, raw_content: str) -> dict:
    """Imports a skill from an arbitrary local file (Brain tab's "import from
    file" — David's ask, 2026-08-31, via Electron's native file picker;
    electron/main.js's ipcMain handler does the actual filesystem read, this
    just parses whatever text comes back).

    Unlike create_skill(), this upserts rather than rejecting a duplicate
    name — re-importing the same file (e.g. after editing it externally) is
    a normal, expected action for a file-backed import flow, not an error.
    If the file already has SKILL.md-shaped frontmatter, its description is
    used; otherwise the whole file becomes the body with no description.
    """
    name = os.path.splitext(os.path.basename(filename))[0]
    parsed = _parse(raw_content)
    slug = _slugify(name)
    path = _skill_path(slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(parsed["description"], parsed["body"], parsed.get("frontmatter", "")))
    return {"slug": slug, "description": parsed["description"], "body": parsed["body"]}


def delete_skill(slug: str) -> None:
    path = _skill_path(slug)
    if os.path.exists(path):
        os.remove(path)
        skill_dir = os.path.dirname(path)
        if not os.listdir(skill_dir):
            os.rmdir(skill_dir)
