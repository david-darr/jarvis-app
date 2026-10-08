"""Turn a person's computer demonstration into an editable, untrusted skill."""
import re
from copy import deepcopy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core import computer

# Reset links, sign-in links and many sites carry secrets in the address
# (?token=, &code=, #access_token=). A skill is saved to disk and shown to
# models, so those values never reach it; the parameter names stay, so the
# step still reads sensibly.
SENSITIVE_PARAM = re.compile(
    r"token|code|key|secret|passw|pwd|session|sid|auth|sig|otp|ticket|nonce|state|credential|jwt|bearer", re.I)
REDACTED = "REDACTED"


def safe_url(url: str) -> str:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return url or ""
    host = parts.hostname or ""
    netloc = host + (f":{parts.port}" if parts.port else "")  # drops any user:password@
    def clean(query: str) -> str:
        pairs = parse_qsl(query, keep_blank_values=True)
        return urlencode([(k, REDACTED if SENSITIVE_PARAM.search(k) else v) for k, v in pairs], safe="/:")
    fragment = clean(parts.fragment) if "=" in parts.fragment else parts.fragment
    return urlunsplit((parts.scheme, netloc, parts.path, clean(parts.query), fragment))


def normalize_steps(steps: list[dict]) -> list[dict]:
    result = []
    for original in steps[:computer.RECORDING_LIMIT]:
        step = deepcopy(original)
        if step.get("url"):
            step["url"] = safe_url(step["url"])
        if step.get("private"):
            step.pop("text", None)
            step.pop("key", None)
        previous = result[-1] if result else None
        same_target = previous and all(previous.get(key) == step.get(key)
            for key in ("kind", "url", "element", "private", "ask_each_time"))
        if same_target:
            before, after = previous.get("window") or {}, step.get("window") or {}
            same_target = (before.get("class") == after.get("class") and
                           before.get("title", "").lstrip('*') == after.get("title", "").lstrip('*'))
        if same_target and step.get("kind") == "type" and not step.get("private"):
            previous["text"] = previous.get("text", "") + step.get("text", "")
        elif same_target and step.get("kind") == "scroll":
            for axis in ("dx", "dy"):
                previous[axis] = previous.get(axis, 0) + step.get(axis, 0)
        elif previous and step.get("kind") == "open" and previous == step:
            continue
        else:
            result.append(step)
    return result


def _text(value) -> str:
    return str(value or "").replace("\r", "\\r").replace("\n", "\\n")


def _person_label(info: dict, *, enter: bool = False) -> str | None:
    for control in [info, *(info.get("form_submits", []) if enter else [])]:
        for label in computer._labels(control):
            if (computer.PERSON_ACTION_WORDS.search(label) or computer.DELETE_WORDS.search(label)
                    or not computer._reactions_allowed() and computer.REACTION_WORDS.search(label)):
                return _text(label)
    return None


def step_text(step: dict) -> str:
    kind = step.get("kind")
    info = step.get("element") or {}
    label = _text(info.get("label"))
    window = step.get("window") or {}
    target = (f" in the '{_text(window.get('title'))}' {_text(window.get('class'))} window" if window else "")
    if step.get("private"):
        return f"The person enters the {_text(step.get('field') or 'private field')} themselves."
    if kind == "open": return f"Open {_text(step.get('url'))}."
    if kind == "launch": return f"Launch {_text(step.get('app'))} with the computer's launch action."
    if kind == "click":
        person = _person_label(info)
        if person: return f"The person does this: press '{person}'."
        if label:
            role = info.get("role") or {"a": "link", "input": "field", "textarea": "field"}.get(info.get("tag"), info.get("tag") or "control")
            return f"Click the '{label}' {_text(role)} on {_text(step.get('url'))}."
        return f"Click ({step.get('x', 0)}, {step.get('y', 0)}){target}" + (" relative to that window." if window else f" on {_text(step.get('url'))}.")
    if kind == "type":
        if step.get("ask_each_time"):
            into = f" into '{label}'" if label else target or " into the focused field"
            return f"Ask the person what to type{into}, then type it (ask each time)."
        return f"Type '{_text(step.get('text'))}'" + (f" into '{label}'" if label else target or " into the focused field") + "."
    if kind == "key":
        person = _person_label(info, enter=step.get("key", "").lower() in ("enter", "return"))
        if person: return f"The person does this: press '{person}'."
        return f"Press {_text(step.get('key'))}{target}."
    if kind == "scroll":
        directions = []
        for axis, positive, negative in (("dy", "down", "up"), ("dx", "right", "left")):
            delta = step.get(axis, 0)
            if delta: directions.append(f"{positive if delta > 0 else negative} {abs(delta)} pixels")
        return "Scroll " + (" and ".join(directions) or "the page") + target + "."
    return "Review this step before running it."


def draft(steps: list[dict], *, agent_name=None) -> dict:
    steps = normalize_steps(steps)
    host = next((urlsplit(step.get("url", "")).hostname for step in steps
                 if urlsplit(step.get("url", "")).hostname), "computer")
    action = next((step.get("kind") for step in steps if step.get("kind") != "open"), "task")
    name = f"{host.replace('.', '-')}-{action}"
    description = f"Repeat the task demonstrated on {host}" + (f" with {agent_name}" if agent_name else "") + "."
    lines = [f"{index}. {step_text(step)}" for index, step in enumerate(steps, 1)]
    body = "\n".join(lines) + "\n\n## How to run this\n\nUse the computer tool. Stop for the person where marked; the computer's safety checks still apply.\n"
    return {"name": name, "description": description, "body": body}
