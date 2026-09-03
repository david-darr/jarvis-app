"""Remote access over Tailscale — reach this JARVIS from your phone or
another computer, without exposing anything to the open internet.

David's ask (2026-09-03): users setting up the downloaded app should be able
to turn this on themselves, the way scripts/run_remote.py does it by hand
here. That script stays as the dev/CLI path; this module is the in-app
equivalent, driven from Settings > Remote Access and offered during
onboarding.

Security model, unchanged from the script and deliberately strict:
  - Bind to the machine's own Tailscale interface IP, never 0.0.0.0. The
    listener is reachable only from devices on your tailnet.
  - Serve real HTTPS using a Tailscale-issued certificate for the machine's
    MagicDNS name. Browsers need a genuine secure context, and a self-signed
    cert would train users to click through warnings.
  - Require real accounts. enable() refuses to start unless auth is on and
    an account exists, so the single-trusted-local-user bypass can never be
    what answers a request arriving over the network.

Runs as a second uvicorn server inside this same process (an asyncio task
serving the same FastAPI app) rather than a subprocess. The script needed a
subprocess because it wanted --reload; nothing here does, and an in-process
server means start/stop from the UI is immediate, has no orphan-process
risk, and shares the already-warm application state.

Certificates are written to DATA_DIR/certs, never into the app directory:
they're per-machine secrets that must not end up in a backup, a git commit,
or a packaged build. (The original script wrote them into scripts/, which is
exactly how they nearly shipped inside an installer — see the 2026-09-03
packaging audit.)
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

from core import events, settings as settings_store
from core.auth import auth_enabled, auth_manager
from core.constants import DATA_DIR

logger = logging.getLogger(__name__)

CERTS_DIR = os.path.join(DATA_DIR, "certs")

_WINDOWS_DEFAULT = r"C:\Program Files\Tailscale\tailscale.exe"
_MACOS_DEFAULT = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

_server = None          # uvicorn.Server
_server_task: asyncio.Task | None = None
_current_url: Optional[str] = None


# -- Tailscale discovery ---------------------------------------------------

def tailscale_exe() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in (_WINDOWS_DEFAULT, _MACOS_DEFAULT, "/usr/bin/tailscale"):
        if os.path.exists(candidate):
            return candidate
    return None


def _run_tailscale(args: list[str], timeout: int = 15) -> Optional[subprocess.CompletedProcess]:
    exe = tailscale_exe()
    if not exe:
        return None
    try:
        return subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("remote_access: tailscale %s failed: %s", " ".join(args), e)
        return None


def _status_json() -> Optional[dict]:
    result = _run_tailscale(["status", "--json"])
    if result is None or result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def detect() -> dict:
    """Everything the setup UI needs to tell the user exactly which step
    they're stuck on, rather than one opaque "couldn't start" failure.
    Every field is safe to show: no keys, no auth tokens."""
    exe = tailscale_exe()
    info = {
        "installed": exe is not None,
        "running": False,
        "logged_in": False,
        "ip": None,
        "hostname": None,
        "has_cert": False,
        "auth_ready": auth_enabled() and auth_manager.has_any_users(),
        "enabled": bool(settings_store.get_setting("remote_access_enabled")),
        "running_now": is_running(),
        "url": _current_url,
        "port": settings_store.get_setting("remote_access_port") or 8422,
    }
    if not exe:
        return info

    status = _status_json()
    if status is None:
        return info
    info["running"] = True

    backend_state = status.get("BackendState", "")
    info["logged_in"] = backend_state == "Running"

    self_node = status.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    ipv4 = next((ip for ip in ips if ":" not in ip), None)
    info["ip"] = ipv4
    hostname = (self_node.get("DNSName") or "").rstrip(".")
    info["hostname"] = hostname or None

    if hostname:
        cert_path, key_path = _cert_paths(hostname)
        info["has_cert"] = os.path.exists(cert_path) and os.path.exists(key_path)
    return info


# -- certificates ----------------------------------------------------------

def _cert_paths(hostname: str) -> tuple[str, str]:
    return (
        os.path.join(CERTS_DIR, f"{hostname}.crt"),
        os.path.join(CERTS_DIR, f"{hostname}.key"),
    )


def provision_cert(hostname: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (cert_path, key_path, error). Reuses an existing pair rather
    than re-requesting: Tailscale rate-limits cert issuance."""
    os.makedirs(CERTS_DIR, exist_ok=True)
    cert_path, key_path = _cert_paths(hostname)
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path, None

    result = _run_tailscale(
        ["cert", "--cert-file", cert_path, "--key-file", key_path, hostname],
        timeout=90,
    )
    if result is None:
        return None, None, "Couldn't run the Tailscale command."
    if result.returncode != 0 or not (os.path.exists(cert_path) and os.path.exists(key_path)):
        detail = (result.stderr or result.stdout or "").strip()
        if "HTTPS" in detail or "https" in detail:
            return None, None, (
                "HTTPS certificates aren't enabled for your tailnet. Turn them on at "
                "login.tailscale.com/admin/dns (under HTTPS Certificates), then try again."
            )
        return None, None, detail or "Tailscale couldn't issue a certificate."
    return cert_path, key_path, None


# -- the listener ----------------------------------------------------------

def is_running() -> bool:
    return _server_task is not None and not _server_task.done()


async def start() -> dict:
    """Bring up the HTTPS listener. Returns {"ok": bool, "url"/"error": ...}.
    Safe to call when already running (no-op)."""
    global _server, _server_task, _current_url

    if is_running():
        return {"ok": True, "url": _current_url}

    info = detect()
    if not info["installed"]:
        return {"ok": False, "error": "Tailscale isn't installed on this machine."}
    if not info["logged_in"]:
        return {"ok": False, "error": "Tailscale is installed but not signed in. Open Tailscale and log in, then try again."}
    if not info["ip"] or not info["hostname"]:
        return {"ok": False, "error": "Couldn't determine this machine's Tailscale address."}
    # The hard gate: never expose the app to a network while it would answer
    # with the no-login local-user bypass.
    if not (auth_enabled() and auth_manager.has_any_users()):
        return {"ok": False, "error": "Create an account first — remote access can't be turned on without a login."}

    cert_path, key_path, err = provision_cert(info["hostname"])
    if err:
        return {"ok": False, "error": err}

    # Imported here, not at module scope: uvicorn is only needed when remote
    # access is actually switched on.
    import uvicorn
    from app import app as fastapi_app

    port = int(settings_store.get_setting("remote_access_port") or 8422)
    config = uvicorn.Config(
        fastapi_app,
        host=info["ip"],
        port=port,
        ssl_certfile=cert_path,
        ssl_keyfile=key_path,
        log_level="info",
        # This server shares the running event loop; uvicorn must not try to
        # install its own signal handlers on a loop it doesn't own.
        lifespan="off",
    )
    _server = uvicorn.Server(config)
    _server.install_signal_handlers = lambda: None

    _server_task = asyncio.create_task(_server.serve())
    # Give it a moment to bind so a port conflict surfaces here, as a real
    # error in the UI, rather than silently in a background task.
    await asyncio.sleep(1.0)
    if _server_task.done():
        exc = _server_task.exception()
        _server_task = None
        _server = None
        return {"ok": False, "error": f"Couldn't start the listener: {exc}"}

    _current_url = f"https://{info['hostname']}:{port}"
    events.emit("remote.enabled", f"Remote access on at {_current_url}")
    logger.info("remote_access: listening on %s (%s:%s)", _current_url, info["ip"], port)
    return {"ok": True, "url": _current_url}


async def stop() -> None:
    global _server, _server_task, _current_url
    if _server is not None:
        _server.should_exit = True
    if _server_task is not None:
        try:
            await asyncio.wait_for(_server_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _server_task.cancel()
        except Exception:
            logger.exception("remote_access: error stopping the listener")
    if _current_url:
        events.emit("remote.disabled", "Remote access turned off")
    _server = None
    _server_task = None
    _current_url = None


async def start_if_enabled() -> None:
    """Called once at app startup so the setting survives a restart — the
    whole point of a persisted toggle. Never raises: a machine that has since
    dropped off the tailnet should still boot the app normally."""
    if not settings_store.get_setting("remote_access_enabled"):
        return
    try:
        result = await start()
        if not result["ok"]:
            logger.warning("remote_access: enabled but couldn't start — %s", result["error"])
            events.emit("remote.failed", f"Remote access couldn't start: {result['error']}", level="warn")
    except Exception:
        logger.exception("remote_access: unexpected error during startup")
