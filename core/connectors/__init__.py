"""Messaging connectors beyond Discord (2026-10-05). See base.py for the
adapter shape, hub.py for how messages are handled, and the vault spec
"Connectors - Hermes Platforms (Build Spec)" for what was ported and why."""
from core.connectors.hub import Hub

KINDS: dict = {}


def register(cls):
    KINDS[cls.kind] = cls
    return cls


# Each platform module registers itself on import.
from core.connectors import platforms  # noqa: E402,F401

hub = Hub(KINDS)
