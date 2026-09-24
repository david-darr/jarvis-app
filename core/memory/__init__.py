"""Memory behind an interface (Hermes track, 2026-09-23).

Where a model looks things up in JARVIS's long-term memory. Every backend
answers the same two calls - search, then read - so a different kind of
memory (a semantic index, say) can replace or join the vault without its
callers changing. Modelled on Hermes Agent's agent/memory_provider.py, but
deliberately slim: Hermes's providers also prefetch memory into every turn,
while JARVIS lets the model decide when to look, which keeps each chat's
cached prompt unchanged turn to turn.

The vault (core/memory/vault.py) is the first backend. Its callers go through
core/memory_tools.py's search_vault/read_vault_file, which the model tools
(core/tool_registry.py) and Swarm's bounded retrieval (core/swarm/memory.py)
already use.
"""
from abc import ABC, abstractmethod
from typing import Optional


class MemoryBackend(ABC):
    """One place memory lives."""

    name: str = "memory"

    @abstractmethod
    def is_available(self) -> bool:
        """Whether it can answer at all right now (a vault folder that exists)."""

    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Ranked hits, best first: {"ref", "title", "snippet"}. `ref` is what
        read() takes; snippets are short, never whole documents."""

    @abstractmethod
    def read(self, ref: str, max_chars: Optional[int] = None) -> str:
        """One item in full, up to max_chars, saying so when it had to cut."""


def vault_memory(vault_dir: Optional[str] = None) -> "MemoryBackend":
    """The vault backend for the vault in use (or the one given)."""
    from core.memory.vault import VaultMemory
    from core.vault import resolve_vault_dir
    return VaultMemory.for_dir(vault_dir or resolve_vault_dir())
