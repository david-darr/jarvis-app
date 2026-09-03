"""Run the JARVIS backend on a Tailscale-only HTTPS listener for remote access.

Mirrors The Bridge's (voice-visualizer/server.py) remote-access pattern: bind
directly to the machine's Tailscale interface IP (never 0.0.0.0), so the
service is reachable only over the Tailscale network, not the open internet.
Uses a real Tailscale-issued HTTPS cert (per-tailnet, auto-trusted) rather
than a self-signed one, since browsers need a genuine secure context.

AUTH_ENABLED is forced on here regardless of the local .env setting -
remote access must never fall back to jarvis-app's single-trusted-local-user
bypass. First run over this listener will show the real login/setup screen
(core/auth.py) - this script never sets a password for the user.

This is a real feature, not a one-off dev script: exposing the backend to
another device over Tailscale is meant to become an onboarding-time option
once JARVIS ships publicly (see JARVIS Plan's v2 scoping), not something a
user has to find and run by hand. Kept in scripts/ for now since onboarding
UI for it doesn't exist yet.
"""
import json
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)

TAILSCALE_EXE = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"

REMOTE_PORT = int(os.getenv("JARVIS_REMOTE_PORT", "8422"))


def resolve_tailscale_ip():
    try:
        result = subprocess.run(
            [TAILSCALE_EXE, "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    ip = result.stdout.strip()
    return ip if result.returncode == 0 and ip else None


def resolve_tailscale_hostname():
    try:
        result = subprocess.run(
            [TAILSCALE_EXE, "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        name = data.get("Self", {}).get("DNSName", "").rstrip(".")
        return name or None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def resolve_tailscale_cert(hostname):
    cert_path = os.path.join(SCRIPT_DIR, f"{hostname}.crt")
    key_path = os.path.join(SCRIPT_DIR, f"{hostname}.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    try:
        result = subprocess.run(
            [TAILSCALE_EXE, "cert", hostname],
            capture_output=True, text=True, timeout=30, cwd=SCRIPT_DIR,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not (os.path.exists(cert_path) and os.path.exists(key_path)):
        print(f"[run_remote] tailscale cert failed: {result.stderr.strip()}")
        return None
    return cert_path, key_path


def main():
    ip = resolve_tailscale_ip()
    if not ip:
        print("[run_remote] Tailscale not signed in or not installed - can't start remote listener.")
        sys.exit(1)

    hostname = resolve_tailscale_hostname()
    if not hostname:
        print("[run_remote] Couldn't resolve Tailscale MagicDNS hostname - can't fetch an HTTPS cert.")
        sys.exit(1)

    cert = resolve_tailscale_cert(hostname)
    if not cert:
        print("[run_remote] Tailscale HTTPS isn't enabled for this tailnet (or cert fetch failed).")
        print("Enable it at https://login.tailscale.com/admin/dns -> HTTPS Certificates, then retry.")
        sys.exit(1)
    cert_path, key_path = cert

    env = dict(os.environ)
    env["AUTH_ENABLED"] = "true"

    url = f"https://{hostname}:{REMOTE_PORT}"
    print(f"[run_remote] Starting remote listener at {url}")
    print("[run_remote] First open will show the JARVIS account setup screen - set your own login there.")

    # Shells out to the real `uvicorn` CLI rather than calling uvicorn.run()
    # in-process (dev convenience, David's ask 2026-08-31, reload=True to
    # match `uvicorn --reload`). Found live: uvicorn's reloader re-executes
    # whatever process/command it was launched as - via the CLI that's just
    # "uvicorn app:app" (cheap re-import), but calling uvicorn.run() from
    # inside this script made the reloader re-run this *entire script*
    # (Tailscale IP/hostname/cert resolution and all) on every reload, which
    # hung instead of restarting. The CLI subprocess only ever re-imports
    # app:app, so this is the correct, non-recursive way to get reload here.
    # Watches only core/routes/services/static (not the whole repo) rather
    # than BASE_DIR + --reload-exclude globs - those globs came back
    # expanded into literal filenames in this environment (a real bug hit
    # live, not worth chasing further) rather than passed through as
    # patterns. Trade-off: app.py itself (thin wiring, rarely touched) needs
    # a manual restart to pick up changes; everything else auto-reloads.
    cmd = [
        sys.executable, "-m", "uvicorn", "app:app",
        "--host", ip,
        "--port", str(REMOTE_PORT),
        "--ssl-certfile", cert_path,
        "--ssl-keyfile", key_path,
    ]
    # Known issue, found live 2026-08-31: --reload reliably detects file
    # changes (WatchFiles logs "Reloading...") but the replacement worker
    # never finishes starting - reproduced 3x with a from-scratch process
    # each time, not a fluke. Root cause not yet isolated (candidates: the
    # spawn-based reload supervisor not surviving this session's
    # nohup-backgrounded/no-real-console launch, vs. a genuine Windows
    # reload bug independent of that - not distinguished yet). Defaulting
    # to reload OFF until this is actually root-caused and re-verified with
    # a full restart-and-confirm cycle - JARVIS_REMOTE_RELOAD=true opts back
    # in for whoever wants to help debug it. Until then, restart this
    # script by hand after backend changes.
    if os.getenv("JARVIS_REMOTE_RELOAD", "false").lower() in {"1", "true", "yes"}:
        cmd += [
            "--reload",
            "--reload-dir", os.path.join(BASE_DIR, "core"),
            "--reload-dir", os.path.join(BASE_DIR, "routes"),
            "--reload-dir", os.path.join(BASE_DIR, "services"),
            "--reload-dir", os.path.join(BASE_DIR, "static"),
        ]
    else:
        print("[run_remote] Auto-reload is off (known hang, not yet fixed - see comment above). "
              "Restart this script manually after backend changes.")

    subprocess.run(cmd, cwd=BASE_DIR, env=env)


if __name__ == "__main__":
    main()
