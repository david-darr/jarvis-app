"""Start a local (stdio) MCP server with a clean environment (roadmap phase
6, 2026-10-06; spec: the vault note "Skills and Integrations - Phase 6
(Build Spec)").

    python -I clean_launch.py -- <command> [args...]

The server gets only the safe system variables below - the list Hermes Agent
gives its stdio MCP servers (tools/mcp_tool_config.py, MIT) - plus its own
MCP_API_KEY when it has one. Whoever starts it (Claude Code, which passes on
the whole environment it was given, or JARVIS's own MCP client) can no longer
hand it every key and token in that environment. Its stdin and stdout are
this process's, so the MCP conversation passes straight through; its exit
code is this process's exit code.
"""
import os
import shutil
import subprocess
import sys

SAFE = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
    # Windows system and profile folders, which launchers such as npx need.
    "ALLUSERSPROFILE", "APPDATA", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "COMPUTERNAME", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS", "OS",
    "PATHEXT", "PROCESSOR_ARCHITECTURE", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PUBLIC", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERDOMAIN", "USERNAME", "USERPROFILE", "WINDIR",
})
OWN = ("MCP_API_KEY",)


def clean_environment(environ) -> dict:
    return {k: v for k, v in environ.items()
            if k.upper() in SAFE or k.upper().startswith(("LC_", "XDG_")) or k in OWN}


def main(argv: list[str]) -> int:
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("clean_launch: no command given", file=sys.stderr)
        return 2
    env = clean_environment(os.environ)
    # Found on the cleaned PATH, so a Windows shim (npx.cmd) resolves too.
    command = shutil.which(argv[0], path=env.get("PATH") or env.get("Path")) or argv[0]
    try:
        return subprocess.call([command, *argv[1:]], env=env)
    except OSError as e:
        print(f"clean_launch: could not start {argv[0]}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
