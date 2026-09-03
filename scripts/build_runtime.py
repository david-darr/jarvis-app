"""Build the self-contained Python runtime that ships inside the packaged app.

David's requirement (2026-09-03): "download the app just like any other
mainstream app (Claude, Discord, Epic Games) and have it work out of the
box." Before this, electron/main.js shelled out to whatever Python happened
to be on the user's PATH — so on a clean machine the packaged app just
showed "backend didn't start." This produces a real interpreter with every
dependency preinstalled, bundled into the installer as an extraResource.

Approach: the official Windows *embeddable* Python distribution, not a
PyInstaller freeze. Two concrete reasons, both specific to this codebase:

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

Usage:
    python scripts/build_runtime.py [--force]
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PYTHON_VERSION = "3.12.10"
EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
RUNTIME_DIR = os.path.join(BASE_DIR, "electron", "runtime")
REQUIREMENTS = os.path.join(BASE_DIR, "requirements.txt")


def log(msg: str) -> None:
    print(f"[build_runtime] {msg}", flush=True)


def download(url: str) -> bytes:
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=180) as resp:
        return resp.read()


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="rebuild even if a runtime already exists")
    args = parser.parse_args()

    python_exe = os.path.join(RUNTIME_DIR, "python.exe")

    if os.path.exists(python_exe) and not args.force:
        log("runtime already present — verifying it instead of rebuilding (use --force to rebuild)")
        verify(python_exe)
        log("runtime OK")
        return 0

    if os.path.exists(RUNTIME_DIR):
        log("removing existing runtime")
        shutil.rmtree(RUNTIME_DIR)
    os.makedirs(RUNTIME_DIR, exist_ok=True)

    zip_bytes = download(EMBED_URL)
    log(f"extracting embeddable Python {PYTHON_VERSION}")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(RUNTIME_DIR)

    enable_site_packages(RUNTIME_DIR)

    get_pip = os.path.join(RUNTIME_DIR, "get-pip.py")
    with open(get_pip, "wb") as f:
        f.write(download(GET_PIP_URL))
    log("bootstrapping pip")
    run(python_exe, [get_pip, "--no-warn-script-location"])
    os.remove(get_pip)

    log("installing requirements (this takes a few minutes)")
    run(python_exe, ["-m", "pip", "install", "--no-warn-script-location", "-r", REQUIREMENTS])

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
