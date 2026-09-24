"""The egress filter for networked sandbox runs (Hermes phase 7, 2026-09-24).

core/sandbox.py runs with no network. Some runs need the internet - fetching
a package, the read-only browser of step 3 - but on Docker Desktop an
ordinarily networked container reaches services on this computer's
loopback, JARVIS's own API among them (scripts/test_sandbox.py proves it).
So a networked run gets no route of its own:

- It joins `jarvis-sbx-internal`, a Docker network created `--internal`:
  no route out and no outside DNS (checked on this machine).
- The only other thing on that network is this filter: a small proxy in its
  own hardened container, also on Docker's ordinary bridge. The run's
  HTTP_PROXY/HTTPS_PROXY point at it, and nothing else is reachable.
- The proxy resolves each destination itself and refuses any that is not a
  public address - loopback, private ranges, link-local (cloud metadata),
  carrier-grade NAT (Tailscale), Docker's own - and any port but 80 and 443.
  It then connects to the address it checked, so a name cannot resolve to
  something public for the check and something private for the connection.
  A redirect is a new request through the proxy and is checked again.

Hermes Agent routes its Docker backend through an egress proxy for the same
reason (tools/environments/docker_egress.py); this one is stdlib-only and
runs from source passed on the command line, so nothing extra is mounted.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

NETWORK = "jarvis-sbx-internal"
PROXY_NAME = "jarvis-sbx-egress"
PROXY_PORT = 8888
PROXY_URL = f"http://{PROXY_NAME}:{PROXY_PORT}"
LABEL = "jarvis-sandbox-egress"  # not core/sandbox.py's LABEL: runs are counted apart from the filter

PROXY_SOURCE = r'''
import asyncio, ipaddress, socket
from urllib.parse import urlsplit

PORTS = {80, 443}


def public(ip):
    a = ipaddress.ip_address(ip.split("%")[0])
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


async def refuse(w, why):
    print("BLOCKED", why, flush=True)
    try:
        w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                b"Blocked by the JARVIS sandbox egress filter: " + why.encode() + b"\n")
        await w.drain()
    finally:
        w.close()


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(r, w):
    try:
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 30)
    except Exception:
        w.close()
        return
    line, _, rest = head.partition(b"\r\n")
    try:
        method, target, version = line.decode("latin-1").split(" ", 2)
    except ValueError:
        return await refuse(w, "malformed request")
    if method == "CONNECT":
        host, _, port = target.rpartition(":")
        forward = None
    else:
        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            return await refuse(w, "only absolute http:// requests or CONNECT")
        host, port = u.hostname, str(u.port or 80)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        keep = [h for h in rest[:-4].split(b"\r\n")
                if h and not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
        forward = (f"{method} {path} {version}\r\n".encode() + b"\r\n".join(keep + [b"Connection: close"]) + b"\r\n\r\n")
    host = host.strip("[]")
    try:
        port = int(port)
    except ValueError:
        return await refuse(w, "bad port")
    if port not in PORTS:
        return await refuse(w, f"port {port} is not 80 or 443")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return await refuse(w, f"cannot resolve {host}")
    ips = sorted({i[4][0] for i in infos})
    if not ips or not all(public(ip) for ip in ips):
        return await refuse(w, f"{host} is a private or local address {ips}")
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(ips[0], port), 15)
    except Exception:
        return await refuse(w, f"cannot connect to {host}")
    print("ALLOWED", method, host, port, ips[0], flush=True)
    if forward is None:
        w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await w.drain()
    else:
        uw.write(forward)
        await uw.drain()
    await asyncio.gather(pipe(r, uw), pipe(ur, w))


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT_NUMBER)
    print("egress filter listening", flush=True)
    async with server:
        await server.serve_forever()

asyncio.run(main())
'''.replace("PORT_NUMBER", str(PROXY_PORT))


async def _docker(*args: str, timeout: float = 60) -> tuple[int, str, str]:
    from core.sandbox import _docker as docker
    return await docker(*args, timeout=timeout)


def run_args() -> list[str]:
    """What a networked run uses in place of `--network none`."""
    return ["--network", NETWORK,
            "-e", f"HTTP_PROXY={PROXY_URL}", "-e", f"HTTPS_PROXY={PROXY_URL}",
            "-e", f"http_proxy={PROXY_URL}", "-e", f"https_proxy={PROXY_URL}",
            "-e", "NO_PROXY=", "-e", "no_proxy="]


_lock = asyncio.Lock()


async def ensure(image: str) -> None:
    """Create the internal network and start the filter if they are not
    there. Raises RuntimeError when either cannot be set up; a networked run
    then does not happen."""
    async with _lock:
        code, out, _ = await _docker("network", "inspect", NETWORK, "--format", "{{.Internal}}")
        if code != 0:
            code, _, err = await _docker("network", "create", "--internal", "--label", LABEL, NETWORK)
            if code != 0:
                raise RuntimeError(f"could not create the sandbox network: {err.strip()[:200]}")
        elif out.strip() != "true":
            raise RuntimeError(f"a Docker network named {NETWORK} exists and is not internal")
        code, out, _ = await _docker("inspect", PROXY_NAME, "--format", "{{.State.Running}}")
        if code == 0 and out.strip() == "true":
            return
        if code == 0:
            await _docker("rm", "-f", PROXY_NAME)
        code, _, err = await _docker(
            "run", "-d", "--rm", "--name", PROXY_NAME, "--label", LABEL, "--network", "bridge",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--user", "65534:65534", "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m",
            image, "python", "-c", PROXY_SOURCE)
        if code != 0:
            raise RuntimeError(f"could not start the egress filter: {err.strip()[:200]}")
        code, _, err = await _docker("network", "connect", NETWORK, PROXY_NAME)
        if code != 0:
            await _docker("rm", "-f", PROXY_NAME)
            raise RuntimeError(f"could not attach the egress filter: {err.strip()[:200]}")
        for _ in range(50):  # a run started before it listens would just fail
            _, logs, _ = await _docker("logs", PROXY_NAME)
            if "listening" in logs:
                logger.info("sandbox egress filter started")
                return
            await asyncio.sleep(0.2)
        await _docker("rm", "-f", PROXY_NAME)
        raise RuntimeError("the egress filter did not start listening")


async def stop() -> None:
    await _docker("rm", "-f", PROXY_NAME)
