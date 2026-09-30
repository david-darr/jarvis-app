"""Fetch a public GitHub SKILL.md for the Tool Store's community install path.

Only GitHub file links are accepted. We turn a blob URL into the exact raw
file host, do not follow redirects, and cap the decoded response before it
reaches the existing skill scanner. This never executes or imports Python.
"""
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx


# Match read_skill's 100,000-character hard stop: UTF-8 bytes are at least
# as large as the decoded character count.
MAX_SKILL_BYTES = 100_000
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")


class RemoteSkillError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteSkill:
    source_url: str
    name: str
    content: str
    sha256: str


def _source(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 2048:
        raise RemoteSkillError("The skill URL is invalid or too long.")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RemoteSkillError("Use a public HTTPS GitHub link to a SKILL.md file.")
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "raw.githubusercontent.com"}:
        raise RemoteSkillError("Skill installs currently accept public GitHub SKILL.md links.")
    segments = [unquote(part) for part in parsed.path.split("/")[1:]]
    if not segments or len(segments) > 32 or any(
        not _SEGMENT.fullmatch(part) or part in {".", ".."} for part in segments
    ):
        raise RemoteSkillError("The GitHub file path is invalid.")
    if host == "github.com":
        if len(segments) < 5 or segments[2] != "blob":
            raise RemoteSkillError("Use a GitHub file link ending in SKILL.md.")
        segments = segments[:2] + segments[3:]
    if len(segments) < 4 or segments[-1] != "SKILL.md":
        raise RemoteSkillError("The link must point to a SKILL.md file.")
    name = segments[-2] if len(segments) > 4 else segments[1]
    return "https://raw.githubusercontent.com/" + "/".join(segments), name


async def fetch_skill(url: str) -> RemoteSkill:
    source_url, name = _source(url)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", source_url) as response:
                if response.status_code != 200:
                    raise RemoteSkillError(f"GitHub returned HTTP {response.status_code} for this skill.")
                parts = []
                size = 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > MAX_SKILL_BYTES:
                        raise RemoteSkillError("This skill is too large to import (100 KB limit).")
                    parts.append(part)
    except httpx.HTTPError as error:
        raise RemoteSkillError(f"Could not download the skill: {error}") from error
    try:
        content = b"".join(parts).decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as error:
        raise RemoteSkillError("The skill file must be UTF-8 text.") from error
    if not content.strip():
        raise RemoteSkillError("The skill file is empty.")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return RemoteSkill(source_url, name, content, digest)
