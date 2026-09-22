# Adapted from Hermes Agent, tools/skill_linter.py at commit
# 42c1a93417 (https://github.com/NousResearch/hermes-agent).
# Kept: the rules that describe any SKILL.md. Dropped: the ones tied to
# Hermes's own ecosystem (its native tool names, metadata.hermes tags, author
# spelling, POSIX script gating) and its shared frontmatter parser, replaced
# here by skills_service's. One deliberate change: Hermes warns on
# descriptions past 60 characters because its skill index truncates there;
# JARVIS lists descriptions whole, so the threshold is a long sentence.
#
# MIT License
#
# Copyright (c) 2025 Nous Research
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Advisory authoring checks for a SKILL.md. Findings never block anything;
they are shown on the skill's card in the Brain tab so the author can act."""

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, List, Optional

_MARKETING_WORDS = (
    "powerful", "comprehensive", "seamless", "advanced", "cutting-edge", "state-of-the-art",
    "revolutionary", "robust")
# Scaffolding files a skill should not ship (noise, not skill content).
_FORBIDDEN_FILES = ("README.md", "CHANGELOG.md", "install.sh", ".env", ".env.example", ".gitignore")
# Presence of the load-bearing section is checked, not exact ordering, so the
# linter is not a change-detector.
_EXPECTED_SECTIONS = ("When to Use", "When to use")
# incident-log-shape: at least this many PR/issue refs AND this density (per 1k chars of prose).
_INCIDENT_REF_MIN = 4
_INCIDENT_REF_PER_KCHAR = 0.5
# A skill is loaded whole and stays in context for the rest of the conversation,
# so body size is paid on every later turn. Hermes's soft budget.
BODY_SOFT_BUDGET_CHARS = 24_000
DESCRIPTION_SOFT_LIMIT_CHARS = 160


@dataclass
class LintFinding:
    severity: str  # "warning" - every rule here is advisory
    rule: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _warn(rule: str, message: str) -> LintFinding:
    return LintFinding("warning", rule, message)


def _strip_code_blocks(body: str) -> str:
    """Remove fenced code blocks so prose-only checks don't fire on examples."""
    return re.sub(r"```.*?```", "", body, flags=re.S)


def _check_description(description: str) -> Iterator[LintFinding]:
    desc = description.strip().strip("'\"")
    if not desc:
        yield _warn("description-missing", "no description; models choose skills by it, so say "
                    "in one sentence what this skill is for.")
        return
    if len(desc) > DESCRIPTION_SOFT_LIMIT_CHARS:
        yield _warn("description-length", f"description is {len(desc)} characters; models pick "
                    "skills from this line, so keep it to one sentence.")
    hits = [w for w in _MARKETING_WORDS if re.search(rf"\b{re.escape(w)}\b", desc.lower())]
    if hits:
        yield _warn("description-marketing",
                    f"description contains marketing words {hits}; state the capability, not adjectives.")


def _check_body(body: str, skill_dir: Optional[Path]) -> Iterator[LintFinding]:
    if len(body) > BODY_SOFT_BUDGET_CHARS:
        yield _warn("oversized-body",
                    f"body is {len(body):,} characters (~{len(body) // 4:,} tokens); a model loads all "
                    f"of it and it stays in context for the rest of the conversation. Keep the always-on "
                    f"rules here and move topic depth into references/<topic>.md, linked from the body.")
    prose = _strip_code_blocks(body)
    if not any(re.search(rf"^#+\s+{re.escape(s)}", body, re.M) for s in _EXPECTED_SECTIONS):
        yield _warn("missing-section", "no '## When to Use' section; skills need explicit "
                    "trigger conditions near the top.")
    refs = len(re.findall(r"(?<![\w/])#\d{3,6}\b|\b(?:PR|issue)\s*#?\d{3,6}\b", prose))
    if refs >= _INCIDENT_REF_MIN and refs / max(len(prose), 1) * 1000 >= _INCIDENT_REF_PER_KCHAR:
        yield _warn("incident-log-shape", f"{refs} PR/issue references in prose; write the generalizable "
                    "rule and why, and drop the incident numbers.")
    if skill_dir is None:
        return
    seen: set = set()
    for match in re.finditer(r"(references|templates|assets)/[\w./-]+", body):
        rel = match.group(0)
        if rel in seen or "*" in rel or rel.endswith("/"):
            continue
        seen.add(rel)
        if not (skill_dir / rel).exists():
            yield _warn("dangling-reference", f"body links '{rel}' but that file is not in the skill's folder.")


def _check_files(skill_dir: Path) -> Iterator[LintFinding]:
    for fname in _FORBIDDEN_FILES:
        if (skill_dir / fname).exists():
            yield _warn("forbidden-file", f"skill ships '{fname}'; skills should not include "
                        "scaffolding or config files.")


def lint(description: str, body: str, skill_dir: Optional[Path] = None) -> List[LintFinding]:
    """Advisory findings for one skill. ``skill_dir`` enables the on-disk checks."""
    findings = list(_check_description(description)) + list(_check_body(body, skill_dir))
    if skill_dir is not None:
        findings += list(_check_files(skill_dir))
    return findings
