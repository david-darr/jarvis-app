"""Adversarial tests for core/sandbox.py (Hermes phase 7 step 1).

Each test tries to get out of the container the way a steered model would:
read the host's files, reach the network or JARVIS's own API, find secrets,
gain privileges, or exhaust the machine. They need Docker running; without
it they are skipped, loudly, and the sandbox itself refuses to run.

    python scripts/test_sandbox.py
"""
import asyncio
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ["JARVIS_DATA_DIR"] = tempfile.mkdtemp(prefix="jarvis-sbx-test-data-")  # never the real data
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import sandbox, tool_registry  # noqa: E402

DOCKER_UP = asyncio.run(sandbox.available())[0]


def run(command, **kw):
    return asyncio.run(sandbox.run(command, **kw))


@unittest.skipUnless(DOCKER_UP, "Docker is not running - the sandbox is unproven on this run")
class EscapeTests(unittest.TestCase):

    def test_runs_unprivileged_on_a_read_only_system(self):
        # CapBnd is the ceiling a process could ever regain; only --cap-drop
        # empties it (CapEff is 0 for nobody anyway). /var/tmp is writable by
        # anyone, so only --read-only stops the write there.
        r = run("id -u; grep -E 'CapEff|CapBnd|NoNewPrivs' /proc/self/status; "
                "touch /etc/owned 2>/dev/null && echo ETC-WRITABLE; touch /var/tmp/owned 2>/dev/null && echo VARTMP-WRITABLE; "
                "touch /work/ok && echo WORK-OK")
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("65534"), r.stdout)
        self.assertIn("CapEff:\t0000000000000000", r.stdout)
        self.assertIn("CapBnd:\t0000000000000000", r.stdout)
        self.assertIn("NoNewPrivs:\t1", r.stdout)
        self.assertNotIn("WRITABLE", r.stdout)
        self.assertIn("WORK-OK", r.stdout)

    def test_the_hosts_files_are_nowhere_in_the_container(self):
        token = f"canary-{uuid.uuid4().hex}"
        outside = Path(tempfile.mkdtemp(prefix="jarvis-canary-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text(token)
        r = run(f"grep -rl --exclude-dir=proc --exclude-dir=sys {token} / 2>/dev/null; "
                "echo ---; cat /proc/mounts; echo ---; ls /run/desktop /mnt/host /host_mnt /mnt/c 2>&1",
                timeout=240)
        found, mounts, listing = r.stdout.split("---\n")
        self.assertEqual(found.strip(), "", "a file outside the workspace was readable")
        host_mounts = [line for line in mounts.splitlines()
                       if line.split()[1] not in ("/", "/work", "/tmp") and not line.split()[1].startswith(("/proc", "/sys", "/dev"))]
        self.assertEqual([m for m in host_mounts if m.split()[1] not in ("/etc/resolv.conf", "/etc/hostname", "/etc/hosts",
                                                                    # --init's tini, read-only from Docker's own VM, not the host
                                                                    "/usr/sbin/docker-init")], [],
                         "only /work comes from the host")
        self.assertEqual(listing.count("No such file or directory"), 4, listing)

    def test_no_network_not_even_to_jarvis_on_this_machine(self):
        server, port = _host_http_server()
        self.addCleanup(server.shutdown)
        probe = ("import socket\n"
                 "for host in ('host.docker.internal', '192.168.65.254', '10.0.2.2', '1.1.1.1', '172.17.0.1'):\n"
                 "    try:\n"
                 f"        socket.create_connection((host, {port} if not host[0].isdigit() or host.startswith('192') or host.startswith('172') or host.startswith('10') else 53), timeout=3)\n"
                 "        print('REACHED', host)\n"
                 "    except OSError as e:\n"
                 "        print('blocked', host, type(e).__name__)\n"
                 "import os; print('interfaces', sorted(os.listdir('/sys/class/net')))\n")
        r = run("python probe.py", files={"probe.py": probe}, timeout=60)
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertNotIn("REACHED", r.stdout)
        self.assertIn("interfaces ['lo']", r.stdout)

    def test_control_a_networked_container_would_reach_jarvis(self):
        """Without --network none the same probe gets through. This is why a
        networked run goes through core/sandbox_egress.py's filter instead, and
        why the test above means something."""
        server, port = _host_http_server()
        self.addCleanup(server.shutdown)
        out = subprocess.run(["docker", "run", "--rm", sandbox.IMAGE, "python", "-c",
                              f"import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:{port}/',timeout=5).status)"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "200", out.stderr[-300:])

    def test_no_secrets_env_or_docker_socket(self):
        token = f"secret-{uuid.uuid4().hex}"
        with patch.dict(os.environ, {"JARVIS_SANDBOX_CANARY": token, "JARVIS_INTERNAL_TOKEN": token}):
            r = run("env; ls -la /var/run/docker.sock /run/docker.sock 2>&1")
        self.assertNotIn(token, r.stdout)
        self.assertEqual(r.stdout.count("No such file or directory"), 2, r.stdout)

    def test_a_fork_bomb_is_contained(self):
        started = time.time()
        r = run(":(){ :|:& };:", timeout=10, pids=64)
        self.assertLess(time.time() - started, 60)
        self.assertNotEqual(r.exit_code, 0)
        after = run("echo alive")
        self.assertEqual(after.stdout.strip(), "alive", "the machine still runs sandboxes afterwards")

    def test_memory_is_capped(self):
        r = run("python -c \"b = bytearray(1024 * 1024 * 1024); print('GOT 1GB')\"", memory="128m", timeout=60)
        self.assertNotIn("GOT 1GB", r.stdout)
        self.assertEqual(r.exit_code, 137, r.stderr)

    def test_a_hung_run_is_killed_on_time_and_leaves_no_container(self):
        started = time.time()
        r = run("sleep 300", timeout=3)
        self.assertLess(time.time() - started, 30)
        self.assertEqual(r.exit_code, 137)
        self.assertIn("time or memory limit", r.stderr)
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={sandbox.LABEL}"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")


@unittest.skipUnless(DOCKER_UP, "Docker is not running")
class WorkspaceTests(unittest.TestCase):

    def test_changes_come_back_as_a_diff_and_the_copy_is_gone(self):
        seen = []
        real_mkdtemp = tempfile.mkdtemp
        with patch("core.sandbox.tempfile.mkdtemp", side_effect=lambda **kw: seen.append(real_mkdtemp(**kw)) or seen[-1]):
            r = run("printf 'one\\nTWO\\n' > a.txt && echo new > sub/b.txt && rm c.txt",
                    files={"a.txt": "one\ntwo\n", "c.txt": "bye\n", "sub/keep.txt": "same\n"})
        self.assertEqual(r.exit_code, 0, r.stderr)
        self.assertEqual(r.changes, [{"path": "a.txt", "status": "modified"}, {"path": "c.txt", "status": "deleted"},
                                     {"path": "sub/b.txt", "status": "added"}])
        self.assertIn("-two\n+TWO", r.diff)
        self.assertFalse(os.path.exists(seen[0]), "the workspace copy is deleted")

    def test_a_copied_folder_leaves_out_links_and_repo_internals(self):
        src = Path(tempfile.mkdtemp(prefix="jarvis-src-"))
        self.addCleanup(shutil.rmtree, src, True)
        (src / "main.py").write_text("print('hi')\n")
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("x")
        outside = Path(tempfile.mkdtemp(prefix="jarvis-out-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "private.txt").write_text("private")
        linked = False
        try:
            os.symlink(outside / "private.txt", src / "link.txt")
            linked = True
        except OSError:
            pass  # Windows without symlink rights; the rest still runs
        r = run("find . -type f | sort; python main.py", source_dir=str(src))
        self.assertIn("./main.py", r.stdout)
        self.assertIn("hi", r.stdout)
        self.assertNotIn(".git", r.stdout)
        if linked:
            self.assertNotIn("link.txt", r.stdout)

    def test_paths_outside_the_workspace_are_refused(self):
        for bad in ("../escape.txt", "/etc/passwd", "C:/Windows/x", "a/../../b"):
            with self.assertRaises(ValueError, msg=bad):
                run("true", files={bad: "x"})


class RefusalTests(unittest.TestCase):

    def test_without_docker_it_refuses_rather_than_running_on_the_host(self):
        async def down(*args, **kw):
            return 1, "", "error during connect"
        with patch("core.sandbox._docker", side_effect=down), \
             patch("asyncio.create_subprocess_exec") as spawn, patch("asyncio.create_subprocess_shell") as shell:
            with self.assertRaises(sandbox.SandboxUnavailable):
                run("echo should-not-run")
            spawn.assert_not_called()
            shell.assert_not_called()

    def test_every_hardening_flag_is_present(self):
        args = sandbox.hardening_args("512m", "1", 128)
        pairs = {args[i]: args[i + 1] for i in range(len(args) - 1)}
        self.assertEqual(pairs["--network"], "none")
        self.assertEqual(pairs["--cap-drop"], "ALL")
        self.assertEqual(pairs["--security-opt"], "no-new-privileges")
        self.assertEqual(pairs["--user"], "65534:65534")
        self.assertEqual(pairs["--memory"], pairs["--memory-swap"])
        self.assertIn("--read-only", args)
        self.assertIn("@sha256:", sandbox.IMAGE, "the image is pinned by digest")


def _fetch_script(targets):
    """A probe run inside a networked sandbox: fetch each URL through the
    proxy the run is given, print what happened."""
    return ("import urllib.request, urllib.error\n"
            f"for url in {targets!r}:\n"
            "    try:\n"
            "        r = urllib.request.urlopen(url, timeout=20)\n"
            "        print('OK', r.status, url)\n"
            "    except urllib.error.HTTPError as e:\n"
            "        print('HTTP', e.code, url, e.read()[:200].decode(errors='replace').strip())\n"
            "    except Exception as e:\n"
            "        print('ERR', type(e).__name__, url, str(e)[:120])\n")


@unittest.skipUnless(DOCKER_UP, "Docker is not running")
class EgressTests(unittest.TestCase):
    """A networked run (network=True) reaches the public internet only through
    core/sandbox_egress.py's filter, and nothing on this computer or its
    network. Needs internet access for the public-site checks."""

    def fetch(self, targets):
        r = run("python probe.py", files={"probe.py": _fetch_script(targets)}, network=True, timeout=120)
        self.assertEqual(r.exit_code, 0, r.stderr)
        return {line.split()[2 if line.startswith(("OK", "HTTP", "ERR")) else 0]: line
                for line in r.stdout.splitlines() if line.startswith(("OK", "HTTP", "ERR"))}

    def test_public_sites_load_over_https_and_http(self):
        out = self.fetch(["https://example.com/", "http://example.com/"])
        self.assertTrue(out["https://example.com/"].startswith("OK 200"), out)
        self.assertTrue(out["http://example.com/"].startswith("OK 200"), out)

    def test_every_private_or_local_destination_is_refused_on_allowed_ports(self):
        targets = ["http://host.docker.internal/", "https://host.docker.internal/",  # this computer
                   "http://127.0.0.1/", "http://localhost/", "http://[::1]/",          # the filter's own loopback
                   "http://192.168.65.254/", "http://172.17.0.1/", "http://10.0.0.1/", "http://192.168.1.1/",
                   "http://169.254.169.254/latest/meta-data/",                         # cloud metadata
                   "http://100.100.100.100/",                                          # Tailscale
                   "http://127.0.0.1.nip.io/"]                                         # a public name for a private address
        out = self.fetch(targets)
        for url in targets:
            if url.startswith("https"):  # a refused tunnel shows only its status; the reason is in the log
                self.assertIn("Tunnel connection failed: 403", out[url])
            elif "nip.io" in url and "cannot resolve" in out[url]:
                pass  # many home routers drop public answers that point at private addresses; refused either way
            else:
                self.assertTrue(out[url].startswith("HTTP 403") and "private or local" in out[url], out[url])
        from core import sandbox_egress
        log = subprocess.run(["docker", "logs", sandbox_egress.PROXY_NAME], capture_output=True, text=True).stdout
        self.assertIn("BLOCKED host.docker.internal is a private or local address", log)
        self.assertNotIn("ALLOWED CONNECT host.docker.internal", log)

    def test_other_ports_are_refused_even_for_this_computer(self):
        server, port = _host_http_server()
        self.addCleanup(server.shutdown)
        out = self.fetch([f"http://host.docker.internal:{port}/", "http://example.com:8080/"])
        for line in out.values():
            self.assertIn("is not 80 or 443", line)

    def test_a_redirect_to_a_private_address_is_refused(self):
        out = self.fetch(["https://httpbin.org/redirect-to?url=http://127.0.0.1/"])
        line = next(iter(out.values()))
        if line.startswith("ERR") or "503" in line or "502" in line:
            self.skipTest(f"httpbin.org unreachable: {line}")
        self.assertTrue(line.startswith("HTTP 403") and "private or local" in line, line)

    def test_no_way_out_but_the_filter(self):
        r = run("getent hosts example.com || echo NO-DNS; "
                "env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy python -c \""
                "import socket\n"
                "for h in ('1.1.1.1', '93.184.215.14'):\n"
                "    try: socket.create_connection((h, 443), timeout=3); print('REACHED', h)\n"
                "    except OSError as e: print('blocked', h, e)\"",
                network=True, timeout=60)
        self.assertIn("NO-DNS", r.stdout)
        self.assertNotIn("REACHED", r.stdout)

    def test_control_only_the_address_rule_stops_the_filter_reaching_this_computer(self):
        """The filter's own container can open a connection to this computer;
        what stops a sandboxed run getting there through it is the address rule."""
        server, port = _host_http_server()
        self.addCleanup(server.shutdown)
        from core import sandbox_egress
        run("true", network=True)  # the filter is up
        out = subprocess.run(["docker", "exec", sandbox_egress.PROXY_NAME, "python", "-c",
                              f"import socket; socket.create_connection(('host.docker.internal', {port}), timeout=5); print('REACHED')"],
                             capture_output=True, text=True, timeout=60)
        self.assertIn("REACHED", out.stdout, out.stderr[-300:])

    def test_the_filter_itself_is_hardened(self):
        from core import sandbox_egress
        run("true", network=True)
        info = subprocess.run(["docker", "inspect", sandbox_egress.PROXY_NAME, "--format",
                               "{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.Config.User}} {{.HostConfig.SecurityOpt}}"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(info, "true [ALL] 65534:65534 [no-new-privileges]")
        internal = subprocess.run(["docker", "network", "inspect", sandbox_egress.NETWORK, "--format", "{{.Internal}}"],
                                  capture_output=True, text=True).stdout.strip()
        self.assertEqual(internal, "true")

    def test_a_run_without_network_is_still_sealed(self):
        r = run("python -c \"import os; print(sorted(os.listdir('/sys/class/net')))\"; env | grep -i proxy || echo NO-PROXY")
        self.assertIn("['lo']", r.stdout)
        self.assertIn("NO-PROXY", r.stdout)


@unittest.skipUnless(DOCKER_UP, "Docker is not running")
class BrowseTests(unittest.TestCase):
    """Step 3: the read-only browser (core/sandbox_browser.py) and the
    prompted internet option of run_code. Needs internet access."""

    def browse(self, url):
        from core import sandbox_browser
        return asyncio.run(sandbox_browser.browse(url))

    def test_a_public_page_comes_back_as_title_text_and_links(self):
        page = self.browse("https://example.com/")
        self.assertIsNone(page["error"])
        self.assertEqual(page["title"], "Example Domain")
        self.assertIn("documentation examples", page["text"])
        self.assertTrue(page["links"] and page["links"][0]["url"].startswith("https://"), page["links"])

    def test_this_computer_and_the_browsers_own_loopback_are_refused(self):
        for url in ("http://host.docker.internal/", "http://127.0.0.1/", "http://localhost:80/", "http://169.254.169.254/"):
            page = self.browse(url)
            # The filter's own refusal, not just an empty page: without
            # --proxy-bypass-list=<-loopback> Chromium reaches its own
            # loopback directly and merely finds nothing there.
            self.assertTrue((page["error"] or "").startswith("refused:"), f"{url}: {page}")
            self.assertEqual(page["text"], "")
        self.assertIsNotNone(self.browse("https://host.docker.internal/")["error"])

    def test_only_web_addresses_are_accepted_and_nothing_is_left_running(self):
        from core import sandbox_browser
        for bad in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "example.com"):
            with self.assertRaises(ValueError, msg=bad):
                self.browse(bad)
        self.browse("https://example.com/")
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={sandbox.LABEL}"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "", "each browser is thrown away with its container")
        self.assertIn("@sha256:", sandbox_browser.BROWSER_IMAGE)

    def test_a_long_page_is_cut_cleanly(self):
        page = self.browse("https://en.wikipedia.org/wiki/Docker_(software)")
        self.assertIsNone(page["error"])
        self.assertTrue(page["text"].endswith("page text cut here"))
        self.assertLessEqual(len(page["links"]), 60)

    def test_browse_is_offered_to_every_model_but_codex_and_claude_needs_no_prompt(self):
        from core.brain import Brain
        from core.external_brain import ExternalBrain
        self.assertIn("browse", [t["function"]["name"] for t in ExternalBrain("http://x", "m", None).tools])
        _, allowed, _ = Brain(vault_dir=tempfile.gettempdir())._tool_config()
        self.assertIn("mcp__hive_mind__browse", allowed)
        text = asyncio.run(tool_registry.call("browse", {"url": "https://example.com/"},
                                              tool_registry.ToolContext("sbx-chat"), tool_registry.OPENAI))
        self.assertTrue(text.startswith("Title: Example Domain"), text[:200])

    def test_run_code_with_internet_asks_first_and_nobody_to_ask_means_no(self):
        from core import permissions
        spawned = []
        real_run = sandbox.run

        async def watched(*a, **kw):
            spawned.append(kw.get("network"))
            return await real_run(*a, **kw)
        fetch = {"command": "python get.py", "internet": True,
                 "files": {"get.py": "import urllib.request as u; print(u.urlopen('https://example.com/', timeout=20).status)"}}
        with patch("core.sandbox.run", side_effect=watched):
            refused = call(fetch)  # no chat window: the broker cannot ask
            self.assertTrue(refused.startswith("Not run:"), refused)
            self.assertEqual(spawned, [], "a refused run never starts")
            with patch.object(permissions, "decide", return_value=permissions.Decision("allow")) as asked:
                allowed = call(fetch)
            self.assertIn("200", allowed)
            self.assertEqual(asked.call_args.kwargs["tool"], "run_code_internet")
            self.assertEqual(spawned, [True])
            with patch.object(permissions, "decide") as not_asked:
                offline = call({"command": "echo offline"})
            not_asked.assert_not_called()
        self.assertIn("offline", offline)


def call(args, is_admin=False):
    return asyncio.run(tool_registry.call("run_code", args, tool_registry.ToolContext("sbx-chat", is_admin),
                                          tool_registry.OPENAI))


class RunCodeToolTests(unittest.TestCase):
    """Step 2: the run_code tool every model gets (not Codex, which has its own sandbox)."""

    def test_every_model_is_offered_it_and_claude_runs_it_without_a_prompt(self):
        from core.brain import Brain
        from core.external_brain import ExternalBrain
        self.assertIn("run_code", [t["function"]["name"] for t in ExternalBrain("http://x", "m", None).tools],
                      "a non-admin local model has it")
        self.assertIn("run_code", [s.name for s in tool_registry.specs(tool_registry.CLAUDE)])
        _, allowed, _ = Brain(vault_dir=tempfile.gettempdir())._tool_config()
        self.assertIn("mcp__hive_mind__run_code", allowed, "pre-approved, as a visible revocable grant")

    @unittest.skipUnless(DOCKER_UP, "Docker is not running")
    def test_a_run_reports_output_and_changes_and_writes_nothing_real(self):
        text = call({"command": "python hi.py > out.txt; cat out.txt", "files": '{"hi.py": "print(6 * 7)"}'})
        self.assertIn("exit_code=0", text)
        self.assertIn("42", text)
        self.assertIn("added: out.txt", text)
        self.assertIn("+42", text)

    @unittest.skipUnless(DOCKER_UP, "Docker is not running")
    def test_only_an_admin_gets_the_jarvis_code_and_never_its_data_or_env(self):
        self.assertIn("only an admin chat", call({"command": "ls", "copy_repo": True}))
        text = call({"command": "test -f core/sandbox.py && echo HAVE-CODE; ls -a; test -e data || echo NO-DATA; "
                                "test -e .env || echo NO-ENV", "copy_repo": True, "timeout_seconds": 300}, is_admin=True)
        self.assertIn("HAVE-CODE", text)
        self.assertIn("NO-DATA", text)
        self.assertIn("NO-ENV", text)

    def test_without_docker_it_says_so_and_runs_nothing(self):
        async def down(*args, **kw):
            return 1, "", "error during connect"
        with patch("core.sandbox._docker", side_effect=down):
            text = call({"command": "echo hi"})
        self.assertIn("Not run: the sandbox is unavailable", text)
        self.assertIn("Nothing was run on this computer", text)

    def test_bad_input_is_refused_before_anything_runs(self):
        self.assertIn("no command", call({"command": " "}))
        self.assertIn("path -> text", call({"command": "ls", "files": "not json"}))
        self.assertIn("path -> text", call({"command": "ls", "files": {"a": 1}}))


def _host_http_server():
    """A stand-in for JARVIS's API, listening on the host."""
    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"jarvis")

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


if __name__ == "__main__":
    if not DOCKER_UP:
        print("WARNING: Docker is not running; the escape tests are skipped and prove nothing.")
    unittest.main(verbosity=2)
