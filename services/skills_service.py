"""Skills: disk-backed SKILL.md files (frontmatter + body), the Brain tab's
skills half. Matches the pattern researched from Odysseus (specs/memory-skills.md)
— portable, user-editable procedure files, not a bespoke DB table.

Auto-extraction from conversations and import-from-URL (both real Odysseus
features) are deliberately out of this pass — this is the CRUD foundation
those would build on top of, not a commitment to build them yet.
"""
import os
import re
from typing import Optional

from core.constants import DATA_DIR

SKILLS_DIR = os.path.join(DATA_DIR, "skills")

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
    for line in frontmatter_text.splitlines():
        if line.startswith("description:"):
            description = line.split(":", 1)[1].strip()
    return {"description": description, "body": body.strip()}


def _render(description: str, body: str) -> str:
    return f"---\ndescription: {description}\n---\n\n{body.strip()}\n"


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
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(description, body))
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
        f.write(_render(parsed["description"], parsed["body"]))
    return {"slug": slug, "description": parsed["description"], "body": parsed["body"]}


def delete_skill(slug: str) -> None:
    path = _skill_path(slug)
    if os.path.exists(path):
        os.remove(path)
        skill_dir = os.path.dirname(path)
        if not os.listdir(skill_dir):
            os.rmdir(skill_dir)
