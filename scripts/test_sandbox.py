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
        """Without --network none the same probe gets through. This is why the
        sandbox has no network mode yet, and why the test above means something."""
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
