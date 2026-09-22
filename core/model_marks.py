"""Which provider logo belongs to an added model.

The logos are LobeHub's single-colour marks (static/img/model-marks/, MIT,
unmodified; see the NOTICE there). Each is a trademark of its owner and is
shown only to identify whose model a row is.

An endpoint does not say who made its model, so this works it out, most
specific evidence first:

1. The CLI kinds are fixed: Claude Code is Claude, Codex is OpenAI's.
2. The model name, because a router or local server hosts many families:
   "anthropic/claude-3.5-sonnet" through OpenRouter is Claude, and a local
   "llama3.1" is Meta's, whatever serves it.
3. The host, for a provider's own API (api.x.ai is Grok) or, failing a
   recognisable model name, the serving platform (OpenRouter, Ollama...).

No match returns None and the UI keeps its generic icon: a wrong logo is
worse than none.
"""
import re
from typing import Optional
from urllib.parse import urlparse

# Model-name families, checked in order; the first pattern that matches wins.
# Word-ish boundaries keep "gpt" from matching inside an unrelated name.
_MODEL_FAMILIES = [
    ("claude", r"claude"),
    ("perplexity", r"sonar|pplx|perplexity"),
    ("grok", r"grok"),
    ("gemini", r"gemini|gemma"),
    ("deepseek", r"deepseek"),
    ("openai", r"(^|[/:\-_ ])(gpt|chatgpt|o1|o3|o4|codex)([\-_.:]|$)|openai|gpt-oss"),
    ("mistral", r"mistral|mixtral|codestral|ministral|magistral|devstral|pixtral"),
    ("qwen", r"qwen|qwq"),
    ("meta", r"llama"),
    ("microsoft", r"(^|[/:\-_ ])phi[\-_.:0-9]"),
    ("cohere", r"command-r|command-a|cohere|aya"),
    ("kimi", r"kimi|moonshot"),
    ("zhipu", r"glm|chatglm|zhipu"),
    ("minimax", r"minimax"),
    ("nvidia", r"nemotron|nvidia"),
]

# Hosts, for a provider's own API or the platform serving an unrecognised model.
_HOSTS = [
    ("claude", ("anthropic.com",)),
    ("openai", ("openai.com", "openai.azure.com")),
    ("grok", ("x.ai",)),
    ("gemini", ("generativelanguage.googleapis.com", "aiplatform.googleapis.com")),
    ("perplexity", ("perplexity.ai",)),
    ("deepseek", ("deepseek.com",)),
    ("mistral", ("mistral.ai",)),
    ("cohere", ("cohere.com", "cohere.ai")),
    ("kimi", ("moonshot.cn", "moonshot.ai")),
    ("zhipu", ("bigmodel.cn", "z.ai")),
    ("minimax", ("minimax.chat", "minimaxi.com")),
    ("openrouter", ("openrouter.ai",)),
    ("groq", ("groq.com",)),
    ("together", ("together.xyz", "together.ai")),
    ("fireworks", ("fireworks.ai",)),
    ("huggingface", ("huggingface.co", "hf.space")),
    ("nvidia", ("nvidia.com",)),
]

# Local servers by their default ports, since their host is just localhost.
_LOCAL_PORTS = {11434: "ollama", 1234: "lmstudio"}

MARKS = sorted({mark for mark, _ in _MODEL_FAMILIES} | {mark for mark, _ in _HOSTS} | set(_LOCAL_PORTS.values()))


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def mark_for(endpoint: dict) -> Optional[str]:
    """The mark id for one endpoint dict (kind, model, base_url, name), or None."""
    kind = endpoint.get("kind")
    if kind == "claude_cli":
        return "claude"
    if kind == "codex_cli":
        return "openai"
    model = (endpoint.get("model") or "").lower()
    for mark, pattern in _MODEL_FAMILIES:
        if model and re.search(pattern, model):
            return mark
    try:
        parsed = urlparse(endpoint.get("base_url") or "")
        host, port = (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        host, port = "", None
    for mark, domains in _HOSTS:
        if any(_host_matches(host, d) for d in domains):
            return mark
    if host in ("localhost", "127.0.0.1", "::1") and port in _LOCAL_PORTS:
        return _LOCAL_PORTS[port]
    if kind == "local" and "ollama" in (endpoint.get("name") or "").lower():
        return "ollama"
    return None
