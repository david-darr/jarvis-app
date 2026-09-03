"""Shared path/constant definitions, imported by app.py and every routes/services module.

Odysseus-style single source of truth for these — new modules should import from
here rather than recomputing paths locally.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")

APP_BIND = os.getenv("APP_BIND", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8420"))

# Full read/write dev access for every connected AI model (David's ask
# 2026-09-01: "I want them to have full access to the whole jarvis-app repo,
# both write and read... so the models can write into items such as events,
# notes, tasks, as well as work on developmental projects"). Deliberately
# excludes data/ (password hashes, live session tokens, encrypted API keys —
# see memory_tools.py's docstring on why that folder never gets raw model
# access) and dist/.venv (build output/vendored deps, not source anyone
# develops against). Shared by core/brain.py (Claude's add_dirs) and
# memory_tools.py's repo file tools (external models' equivalent), so both
# halves of the hive mind see the same app-source surface.
REPO_CODE_DIRNAMES = ["core", "routes", "services", "static", "scripts", "specs", "mcp_servers", "electron"]
REPO_CODE_DIRS = [os.path.join(BASE_DIR, d) for d in REPO_CODE_DIRNAMES]
