"""Contract check per provider, before a release (roadmap phase 8, 2026-10-06;
spec: the vault note "Operations - Phase 8 (Build Spec)"). Opt-in: it spends
one tiny turn per connected model kind, and one more for Claude's tool list.

    python scripts/check_providers.py [--data <JARVIS data folder>] [--kinds claude_cli,codex_cli,local,api]

It starts a throwaway backend on a copy of only your model connections (and
the key that decrypts their API keys) - never your chats or anything else -
and for one connection of each kind asks a new chat to call JARVIS's
list_notes tool. Through the real chat and runs routes it then checks the
run contract JARVIS relies on (core/runs.py):

- the run finished, with its usage reported;
- the tool call was recorded started and finished, in pairs, in the run's
  timeline (run_events).

It also reports drift that used to be caught by hand:

- each CLI's installed version against the one JARVIS verified (Swarm's
  Codex gate; the Claude CLI JARVIS would launch, core/claude_cli.py);
- Claude Code's real built-in tool list, read from its own start message,
  against Settings > Agent Tools' hand-kept AGENT_TOOLS.

Exit code 1 when a contract check fails; drift is reported, not failed.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROMPT = "Call JARVIS's list_notes tool exactly once, then reply with the single word DONE."


def default_data() -> Path:
    for candidate in (Path(os.environ.get("APPDATA", "")) / "JARVIS-dev" / "data", REPO / "data"):
        if (candidate / "model_endpoints.json").exists():
            return candidate
    return REPO / "data"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base, method, path, body=None, timeout=600):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "detail": e.read().decode()[:300]}


def contract(base: str, endpoint: dict) -> dict:
    sid = call(base, "POST", "/api/sessions", {"title": f"provider check {endpoint['kind']}"})["id"]
    call(base, "POST", f"/api/sessions/{sid}/model", {"model_endpoint_id": endpoint["id"]})
    reply = call(base, "POST", "/api/chat", {"session_id": sid, "message": PROMPT})
    runs = call(base, "GET", f"/api/runs?session_id={sid}")
    problems, steps = [], []
    if not isinstance(runs, list) or not runs:
        problems.append(f"no run recorded ({reply})")
        return {"kind": endpoint["kind"], "name": endpoint["name"], "problems": problems}
    run = call(base, "GET", f"/api/runs/{runs[0]['id']}")
    steps = run.get("steps") or []
    started = [s for s in steps if s["kind"] == "tool_started"]
    finished = [s for s in steps if s["kind"] == "tool_finished"]
    if run.get("outcome") != "finished":
        problems.append(f"the run ended {run.get('outcome')}: {run.get('detail')}")
    if run.get("total_tokens") is None:
        problems.append("no usage was reported")
    if not any("list_notes" in f"{s.get('name')} {s.get('detail')}" for s in started):
        problems.append(f"list_notes was not recorded as a tool call (tools: {[s.get('name') for s in started]})")
    if len(started) != len(finished):
        problems.append(f"{len(started)} tool starts against {len(finished)} finishes")
    return {"kind": endpoint["kind"], "name": endpoint["name"], "model": run.get("model"), "tokens": run.get("total_tokens"),
            "tools": [s.get("name") for s in started], "reply": str(reply.get("reply", reply))[:80], "problems": problems}


def versions() -> list[str]:
    notes = []
    from core.swarm.adapters import codex_worker
    path = codex_worker.codex_path()
    if path:
        found = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        mark = "matches" if found == codex_worker.VERIFIED_CLI_VERSION else "DIFFERS from"
        notes.append(f"Codex CLI {found or '?'} {mark} Swarm's verified {codex_worker.VERIFIED_CLI_VERSION}"
                     + ("" if found == codex_worker.VERIFIED_CLI_VERSION else
                        " - rerun scripts/test_swarm_codex.py --cli before raising it"))
    else:
        notes.append("Codex CLI not found")
    from core import claude_cli
    chosen = claude_cli.preferred_cli_path()
    notes.append(f"Claude CLI JARVIS launches: {chosen or 'the SDK bundled copy'}")
    return notes


def claude_tools() -> list[str]:
    """Claude Code's built-in tools as its own start message lists them,
    against Settings > Agent Tools' hand-kept list."""
    import asyncio
    from claude_agent_sdk import ClaudeAgentOptions, SystemMessage, query
    from core import claude_cli
    from routes.settings_routes import AGENT_TOOLS

    async def tools():
        options = ClaudeAgentOptions(max_turns=1, cli_path=claude_cli.preferred_cli_path(), allowed_tools=[])
        found = []
        # Read to the end: leaving the SDK's stream early raises on close.
        async for message in query(prompt="Reply OK.", options=options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                found = [t for t in message.data.get("tools", []) if not t.startswith("mcp__")]
        return found

    builtin = asyncio.run(tools())
    missing = sorted(set(AGENT_TOOLS) - set(builtin))
    new = sorted(set(builtin) - set(AGENT_TOOLS))
    notes = [f"Claude Code offers {len(builtin)} built-in tools."]
    if missing:
        notes.append(f"In AGENT_TOOLS but no longer offered: {', '.join(missing)}")
    if new:
        notes.append(f"Offered but not in AGENT_TOOLS (Settings > Agent Tools can't switch them off): {', '.join(new)}")
    if not missing and not new:
        notes.append("AGENT_TOOLS matches.")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=None, help="the JARVIS data folder whose model connections to use")
    parser.add_argument("--kinds", default="claude_cli,codex_cli,local,api")
    parser.add_argument("--no-claude-tools", action="store_true", help="skip the extra Claude turn for its tool list")
    args = parser.parse_args()
    source = args.data or default_data()
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-provider-check-"))
    for name in ("model_endpoints.json", ".secret_key"):
        if (source / name).exists():
            shutil.copy2(source / name, scratch / name)
    endpoints = json.loads((scratch / "model_endpoints.json").read_text(encoding="utf-8")) \
        if (scratch / "model_endpoints.json").exists() else {}
    chosen = {}
    for endpoint in endpoints.values():
        if endpoint.get("kind") in kinds and endpoint["kind"] not in chosen:
            chosen[endpoint["kind"]] = endpoint
    print(f"Model connections from {source}: {', '.join(f'{k} ({e['name']})' for k, e in chosen.items()) or 'none'}")
    os.environ.update(JARVIS_DATA_DIR=str(scratch), JARVIS_VAULT_DIR=str(scratch / "vault"))
    sys.path.insert(0, str(REPO))
    port = free_port()
    env = {**os.environ, "APP_PORT": str(port), "JARVIS_INSTANCE": "providercheck"}
    env.pop("JARVIS_UI_SECRET", None)
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                              cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    failed = False
    try:
        for _ in range(90):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)
                break
            except OSError:
                time.sleep(1)
        else:
            print("FAIL: the throwaway backend did not start")
            return 1
        for kind, endpoint in chosen.items():
            result = contract(base, endpoint)
            ok = not result["problems"]
            failed |= not ok
            print(f"{'PASS' if ok else 'FAIL'} {kind} ({endpoint['name']}): "
                  + (f"{result.get('tokens')} tokens, tools {result.get('tools')}" if ok else "; ".join(result["problems"])))
        for note in versions():
            print("VERSION", note)
        if "claude_cli" in chosen and not args.no_claude_tools:
            for note in claude_tools():
                print("TOOLS", note)
    finally:
        subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], capture_output=True) if os.name == "nt" \
            else server.terminate()
        time.sleep(1)
        shutil.rmtree(scratch, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
