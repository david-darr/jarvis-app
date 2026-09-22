"""Build the self-contained Python runtime that ships inside the packaged app.

David's requirement (2026-09-03): "download the app just like any other
mainstream app (Claude, Discord, Epic Games) and have it work out of the
box." Before this, electron/main.js shelled out to whatever Python happened
to be on the user's PATH — so on a clean machine the packaged app just
showed "backend didn't start." This produces a real interpreter with every
dependency preinstalled, bundled into the installer as an extraResource.

Per platform:
  - Windows: python.org's *embeddable* distribution, a relocatable
    interpreter intended for exactly this.
  - macOS: python.org ships no embeddable build, so this uses
    python-build-standalone (maintained by Astral) — relocatable CPython
    with pip already included. Built for the host architecture, so an
    arm64 machine (or runner) produces an arm64 runtime.

Not a PyInstaller freeze, for two reasons specific to this codebase:

  1. core/custom_tabs.py discovers routes/tab_*.py by scanning the directory
     at runtime and importlib-importing what it finds — and Developer Mode
     lets a connected model WRITE new ones into a live install. PyInstaller
     resolves its module graph at build time, so a tab created after
     packaging could never be imported. Freezing would silently break a
     shipped feature.
  2. The dominant size term is claude_agent_sdk's vendored claude.exe
     (~208MB), which is an opaque data blob either way. Freezing the Python
     half saves nothing meaningful against it.

An embedded interpreter keeps runtime semantics byte-identical to dev:
dynamic imports, subprocess spawning, and native wheels all behave the same.

Idempotent: re-running with the runtime already present and healthy is a
no-op unless --force is passed.

Supply chain (2026-09-22, following Hermes Agent's pinning policy): every
download is checked against a SHA-256 pinned here, and the app's packages are
installed from requirements.lock with --require-hashes, so a swapped or
tampered file fails the build instead of shipping inside a signed installer.
Changing PYTHON_VERSION or PBS_RELEASE means updating the matching hashes.

Usage:
    python scripts/build_runtime.py [--force]
"""
import argparse
import hashlib
import io
import json
import os
import re
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# Windows: python.org's "embeddable" zip — a relocatable interpreter meant
# for exactly this.
PYTHON_VERSION = "3.12.10"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
# python.org publishes only an MD5 for this file; this SHA-256 is of the
# download whose MD5 matched its release record (fe8ef205f2e9c3ba44d0cf9954e1abd3).
EMBED_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

# macOS: python.org ships no embeddable build, so use python-build-standalone
# (maintained by Astral) — relocatable CPython with pip already inside. The
# "install_only" tarball is the runtime-only variant, no build artefacts.
PBS_RELEASE = "20260901"
PBS_VERSION = "3.12.14"
# From the release's own SHA256SUMS file.
PBS_SHA256 = {
    "aarch64": "3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76",
    "x86_64": "2e31b23f3f1319f707d0e620b48847a0046577541d357276821f9f1b5492e0ba",
}


def _pbs_arch() -> str:
    return "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def _pbs_url() -> str:
    return (
        f"https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_RELEASE}/cpython-{PBS_VERSION}+{PBS_RELEASE}-{_pbs_arch()}-apple-darwin-install_only.tar.gz"
    )


# A fixed commit of pypa/get-pip rather than bootstrap.pypa.io's moving
# "latest", so the script that runs during the build is a known file.
GET_PIP_URL = ("https://raw.githubusercontent.com/pypa/get-pip/"
               "f6f644156f23dfe9acc06e7b9ca75eee311f2e37/public/get-pip.py")
GET_PIP_SHA256 = "fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6"
# The build's own installer, exact, per Hermes's rule for build-only tooling.
# It is uninstalled from the shipped runtime once the packages are in.
PIP_VERSION = "26.2.1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
RUNTIME_DIR = os.path.join(BASE_DIR, "electron", "runtime")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")
LOCKFILE = os.path.join(BASE_DIR, "requirements.lock")


def log(msg: str) -> None:
    print(f"[build_runtime] {msg}", flush=True)


def download(url: str, sha256: str) -> bytes:
    """Fetch url and refuse it unless its SHA-256 is the pinned one."""
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=180) as resp:
        data = resp.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise RuntimeError(
            f"checksum mismatch for {url}\n  expected {sha256}\n  got      {actual}\n"
            "Refusing to build from a file that is not the one that was pinned."
        )
    return data


def enable_site_packages(runtime_dir: str) -> None:
    """The embeddable distribution ships with a `python3xx._pth` file that
    deliberately disables site-packages (it's meant for embedding, where the
    host app controls sys.path). Uncommenting `import site` is the
    documented way to turn normal package imports back on — without it, pip
    installs succeed but nothing is importable at runtime."""
    pth_files = [f for f in os.listdir(runtime_dir) if f.endswith("._pth")]
    if not pth_files:
        raise RuntimeError("no ._pth file found in the embeddable distribution")
    pth_path = os.path.join(runtime_dir, pth_files[0])
    with open(pth_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    out = []
    for line in lines:
        out.append("import site" if line.strip() in ("#import site", "# import site") else line)
    if "import site" not in out:
        out.append("import site")
    # The app's own source lives one level up from the runtime in the
    # packaged layout (resources/backend/ vs resources/backend/runtime/), and
    # is added explicitly by main.js's cwd anyway — but Lib/site-packages
    # must be on the path for the installed deps to resolve.
    if "Lib\\site-packages" not in out:
        out.append("Lib\\site-packages")

    with open(pth_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    log(f"enabled site-packages in {os.path.basename(pth_path)}")


def run(python_exe: str, args: list[str]) -> None:
    result = subprocess.run([python_exe, *args], cwd=RUNTIME_DIR)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")


def _requirement_lines() -> list:
    """requirements.txt's package lines, with comments and pip directives
    (such as --extra-index-url) removed."""
    lines = []
    with open(REQUIREMENTS, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                lines.append(line)
    return lines


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so discord.py, discord-py and discord_py match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def unbounded_requirements() -> list:
    """requirements.txt lines with no upper bound: neither `<`, `==`, nor a
    direct reference to one exact wheel file (`name @ https://...whl`).

    Hermes's rule, adopted as written: a bare `>=` lets any future release in,
    including a compromised one.
    """
    unbounded = []
    for line in _requirement_lines():
        spec = line.split(";", 1)[0]
        exact_file = re.search(r"@\s*https://\S+\.whl$", spec.strip())
        if "<" not in spec and "==" not in spec and not exact_file:
            unbounded.append(line)
    return unbounded


def locked_versions() -> dict:
    """{normalised name: [versions]} for every entry in requirements.lock that
    carries at least one hash. A package can be listed once per platform
    marker (llama-cpp-python is), hence a list of versions."""
    locked, current, hashed = {}, None, False

    def close_entry():
        if current and hashed:
            locked.setdefault(current[0], []).append(current[1])

    with open(LOCKFILE, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if raw[0].isspace():
                if line.startswith("--hash=sha256:"):
                    hashed = True
                continue
            if line.startswith("-"):
                continue
            close_entry()
            spec = line.rstrip("\\").split(";", 1)[0].strip()
            # `name==version`, or `name @ url` for a direct file reference,
            # in which case the URL stands in for the version.
            if " @ " in spec:
                name, _, version = spec.partition(" @ ")
            else:
                name, _, version = spec.partition("==")
            current = (_normalise(name.split("[", 1)[0]), version.strip()) if version else None
            hashed = False
    close_entry()
    return locked


def check_pinning_policy() -> None:
    """Refuse to build unless requirements.txt is bounded and requirements.lock
    hash-pins every package it names. A lock that has fallen behind the
    requirements would otherwise build a runtime missing a package."""
    unbounded = unbounded_requirements()
    if unbounded:
        raise RuntimeError(
            "requirements.txt has lines without an upper bound: " + "; ".join(unbounded)
        )
    if not os.path.exists(LOCKFILE):
        raise RuntimeError("requirements.lock is missing - regenerate it (command in its header)")
    locked = locked_versions()
    missing = [name for name in required_distributions() if name not in locked]
    if missing:
        raise RuntimeError(
            "requirements.lock does not hash-pin " + ", ".join(missing)
            + " - regenerate it (command in its header) and commit both files"
        )


def required_distributions() -> list:
    """Distribution names from requirements.txt.

    Derived from the file rather than restated in code. A hardcoded list is
    exactly how this check silently rotted: it named nine packages while
    requirements.txt had grown to fourteen, so a runtime missing the newer
    ones verified clean and shipped, and the failure only surfaced on a
    user's machine when they opened a spreadsheet.

    Version pins, extras, environment markers and index directives are all
    stripped. This answers "is the package there at all", which is the thing
    that actually goes wrong.
    """
    names = []
    for line in _requirement_lines():
        name = _normalise(re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0])
        if name and name not in names:
            names.append(name)
    return names


def missing_distributions(python_exe: str) -> list:
    """Which required packages the runtime does NOT have installed.

    Checked through importlib.metadata rather than by importing, because
    distribution names and import names differ often enough (python-docx ->
    docx, discord.py -> discord, llama-cpp-python -> llama_cpp) that a
    name-mapping table would just be a second source of drift.
    """
    probe = "\n".join([
        "import json,sys",
        "from importlib.metadata import distribution, PackageNotFoundError",
        "missing=[]",
        "for name in json.loads(sys.argv[1]):",
        "    try: distribution(name)",
        "    except PackageNotFoundError: missing.append(name)",
        "print(json.dumps(missing))",
    ])
    result = subprocess.run(
        [python_exe, "-c", probe, json.dumps(required_distributions())],
        cwd=BASE_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("runtime dependency check failed:\n" + result.stderr.strip())
    return json.loads(result.stdout.strip() or "[]")


def verify(python_exe: str) -> None:
    """Prove the runtime can actually import the app's real dependency graph
    before we call the build good — a runtime that installs cleanly but
    can't import fastapi is worse than no runtime, because the failure only
    shows up on the user's machine."""
    log("verifying the runtime can import the app's dependencies")
    check = (
        "import fastapi, uvicorn, pydantic, bcrypt, pyotp, cryptography, httpx, discord, "
        "claude_agent_sdk; print('imports OK')"
    )
    result = subprocess.run([python_exe, "-c", check], cwd=BASE_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"runtime verification failed:\n{result.stderr.strip()}")
    log(result.stdout.strip())
    missing = missing_distributions(python_exe)
    if missing:
        raise RuntimeError("runtime is missing required packages: " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rebuild even if a runtime already exists")
    args = parser.parse_args()

    check_pinning_policy()

    # electron/main.js's resolveBackendPython() looks in these exact places.
    python_exe = (os.path.join(RUNTIME_DIR, "python.exe") if IS_WINDOWS
                  else os.path.join(RUNTIME_DIR, "bin", "python3"))

    if os.path.exists(python_exe) and not args.force:
        stale = missing_distributions(python_exe)
        if stale:
            # Self-healing rather than merely loud: a runtime built before a
            # dependency was added to requirements.txt is stale, and someone
            # running `npm run dist` without --force is precisely how that
            # ships broken. Rebuilding here is what stops a release going out
            # with a package missing.
            log("runtime is missing " + ", ".join(stale) + " - rebuilding instead of reusing it")
        else:
            log("runtime already present — verifying it instead of rebuilding (use --force to rebuild)")
            verify(python_exe)
            log("runtime OK")
            return 0

    if not (IS_WINDOWS or IS_MACOS):
        raise RuntimeError(f"no runtime recipe for this platform ({sys.platform})")

    if os.path.exists(RUNTIME_DIR):
        log("removing existing runtime")
        shutil.rmtree(RUNTIME_DIR)
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    if IS_WINDOWS:
        zip_bytes = download(EMBED_URL, EMBED_SHA256)
        log(f"extracting embeddable Python {PYTHON_VERSION}")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(RUNTIME_DIR)

        # Windows-only: the embeddable build ships site-packages disabled.
        enable_site_packages(RUNTIME_DIR)

        get_pip = os.path.join(RUNTIME_DIR, "get-pip.py")
        with open(get_pip, "wb") as f:
            f.write(download(GET_PIP_URL, GET_PIP_SHA256))
        log(f"bootstrapping pip {PIP_VERSION}")
        run(python_exe, [get_pip, "--no-warn-script-location", f"pip=={PIP_VERSION}"])
        os.remove(get_pip)
    else:
        url = _pbs_url()
        tar_bytes = download(url, PBS_SHA256[_pbs_arch()])
        log(f"extracting standalone CPython {PBS_VERSION} ({platform.machine()})")
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
            tf.extractall(RUNTIME_DIR)
        # The tarball contains a top-level "python/" directory; hoist its
        # contents so the layout matches what main.js expects (runtime/bin/python3).
        inner = os.path.join(RUNTIME_DIR, "python")
        if os.path.isdir(inner):
            for entry in os.listdir(inner):
                shutil.move(os.path.join(inner, entry), os.path.join(RUNTIME_DIR, entry))
            os.rmdir(inner)
        if not os.path.exists(python_exe):
            raise RuntimeError(f"expected an interpreter at {python_exe} after extraction")
        # This build already includes pip; bring it to the same exact version.
        run(python_exe, ["-m", "pip", "install", "--no-warn-script-location", f"pip=={PIP_VERSION}"])

    # From the lock, never requirements.txt. --require-hashes makes pip refuse
    # any file whose hash is not in the lock, and --no-deps stops it resolving
    # anything the lock did not already name.
    log("installing requirements.lock with hash checking (this takes a few minutes)")
    run(python_exe, ["-m", "pip", "install", "--no-warn-script-location",
                     "--require-hashes", "--no-deps", "-r", LOCKFILE])

    # pip itself is ~13MB and is never needed at runtime by the shipped app.
    log("removing pip/setuptools from the shipped runtime")
    run(python_exe, ["-m", "pip", "uninstall", "-y", "pip", "setuptools", "wheel"])

    verify(python_exe)

    total = sum(
        os.path.getsize(os.path.join(root, f))
        for root, _, files in os.walk(RUNTIME_DIR)
        for f in files
    )
    log(f"runtime built at {RUNTIME_DIR} ({total / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
