"""Small, stable search bridge for a local/API chat's less-used tools: the
MCP tools discovered for that connection, and on a small window Kairos's own
non-core tools (core/tool_registry.py deferred_tools, 2026-10-06).

Only the three bridge schemas reach the model until it asks for a matching
tool. A bridge call never bypasses the chat's integration scope, permission
broker, lifecycle hooks or audit.
"""
import json
import re


SEARCH = "jarvis_tool_search"
DESCRIBE = "jarvis_tool_describe"
CALL = "jarvis_tool_call"
_WORDS = re.compile(r"[a-z0-9]+")


def bridge_schemas() -> list[dict]:
    """Byte-stable schemas, so a chat does not pay a cache miss as tools change."""
    return [
        _function(SEARCH,
                  "Search more tools: Kairos's own less-used tools (tasks, calendar, documents, Google, "
                  "code and more) and the MCP integrations enabled for this chat. Use an action or service, "
                  "such as 'create task' or 'github create issue'. Returns matching tool names and short "
                  f"descriptions. Then call {DESCRIBE} for arguments and {CALL} to use one. These are real "
                  "tools; do not say one is unavailable before searching.",
                  {"query": {"type": "string", "description": "Service, action, or topic to find"},
                   "limit": {"type": "integer", "description": "Results to return, 1 to 10 (default 5)"}},
                  ["query"]),
        _function(DESCRIBE, "Get the full argument schema for one tool found by jarvis_tool_search.",
                  {"name": {"type": "string", "description": "Exact tool name returned by search"}}, ["name"]),
        _function(CALL, "Use one tool found by jarvis_tool_search. The usual permission decision still applies.",
                  {"name": {"type": "string", "description": "Exact tool name returned by search"},
                   "arguments": {"type": "object", "description": "Arguments matching its schema"}},
                  ["name", "arguments"]),
    ]


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                            "parameters": {"type": "object", "properties": properties,
                                                           "required": required}}}


def search(catalog: dict[str, dict], query: str, limit: int = 5) -> str:
    """Rank only tools this chat actually discovered; never search the global catalog."""
    query = query if isinstance(query, str) else ""
    words = set(_WORDS.findall((query or "").lower()))
    if not words:
        return "Enter a service, action, or topic to search for."
    try:
        count = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        count = 5
    ranked = []
    for name, spec in catalog.items():
        title = f"{spec['server']} {spec['name']}".lower()
        summary = (spec.get("description") or "").lower()
        title_words = set(_WORDS.findall(title))
        summary_words = set(_WORDS.findall(summary))
        matched = words & (title_words | summary_words)
        if not matched:
            continue
        score = len(words & title_words) * 4 + len(words & summary_words)
        if query.lower() in title:
            score += 8
        ranked.append((-score, name, {"name": name, "server": spec["server"],
                                      "description": (spec.get("description") or "")[:300]}))
    ranked.sort()
    return json.dumps([entry for _, _, entry in ranked[:count]], ensure_ascii=False) if ranked else "No matching tools."


def describe(catalog: dict[str, dict], name: str) -> str:
    if not isinstance(name, str):
        return "Tool not available in this chat. Search again for an enabled tool."
    spec = catalog.get(name)
    if spec is None:
        return "Tool not available in this chat. Search again for an enabled tool."
    return json.dumps({"name": name, "server": spec["server"],
                       "description": spec.get("description") or "", "parameters": spec["schema"]},
                      ensure_ascii=False)
