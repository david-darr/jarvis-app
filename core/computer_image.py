"""Build the contained computer's Playwright Python image from a pinned base.

The browser base has Chromium but no Python Playwright package. Docker builds
this image once with hash-checked wheels from PyPI; that build is the one time
Docker uses its normal internet access for computer use. Running computers
still use the filtered sandbox network. No package is installed at run time.
"""
import asyncio
import hashlib

from core import sandbox, sandbox_browser

REQUIREMENTS = (
    "playwright==1.63.0 --hash=sha256:ad21bc07516b187965a7521c5cf0df0bd657b17482eaad74335272d35a2b07de "
    "--hash=sha256:354e15b29503565fc598b89f16fbe070459343bef9d7498a93e304864000c6a7",
    "greenlet==3.5.6 --hash=sha256:975736b002ed080d124cf81a79cb7e05cb26d6b3f5c7a7b651c0fcce70353aa1 "
    "--hash=sha256:e85880b538e59a59f55117b81f208a6660ad5ac328aad9305f812d9b8bc67a0f",
    "pyee==13.0.1 --hash=sha256:af2f8fede4171ef667dfded53f96e2ed0d6e6bd7ee3bb46437f77e3b57689228",
    "typing_extensions==4.16.0 --hash=sha256:481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
)
DOCKERFILE = (
    f"FROM {sandbox_browser.BROWSER_IMAGE}\n"
    "RUN printf '%s\\n' \\\n"
    + "".join(f"    '{requirement}' \\\n" for requirement in REQUIREMENTS)
    + "    > /tmp/computer-requirements.txt \\\n"
    "    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes "
    "-r /tmp/computer-requirements.txt\n"
)
BUILD_SECONDS = 600
_lock = asyncio.Lock()
_desktop_lock = asyncio.Lock()
DESKTOP_BUILD_SECONDS = 900
DESKTOP_APPS = {
    "files": ("pcmanfm", "/profile/Documents"),
    "editor": ("mousepad",),
    "pdf": ("atril",),
    "images": ("ristretto",),
    "writer": ("libreoffice", "--writer"),
    "calc": ("libreoffice", "--calc"),
    "impress": ("libreoffice", "--impress"),
}


def desktop_dockerfile() -> str:
    requirements = (*REQUIREMENTS,
        "pillow==12.3.0 --hash=sha256:78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91 "
        "--hash=sha256:d9c7f76c0673154f044e9d78c8655fb4213f6ca31a836df48b40fe5d187717b9")
    return (
        f"FROM {sandbox_browser.BROWSER_IMAGE}\n"
        "RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources \\\n"
        "    && printf '%s\\n' 'Types: deb' \\\n"
        "    'URIs: https://snapshot.ubuntu.com/ubuntu/20261007T000000Z' \\\n"
        "    'Suites: noble noble-updates noble-security' \\\n"
        "    'Components: main universe' \\\n"
        "    'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \\\n"
        "    'Check-Valid-Until: no' > /etc/apt/sources.list.d/ubuntu.sources \\\n"
        "    && apt-get update \\\n"
        "    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\\n"
        "    openbox xdotool pcmanfm mousepad atril ristretto \\\n"
        "    libreoffice-writer libreoffice-calc libreoffice-impress fonts-dejavu-core \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "RUN printf '%s\\n' \\\n"
        + "".join(f"    '{requirement}' \\\n" for requirement in requirements)
        + "    > /tmp/computer-requirements.txt \\\n"
        "    && python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes "
        "-r /tmp/computer-requirements.txt\n"
    )


def image_tag(dockerfile: str | None = None, desktop: bool = False) -> str:
    dockerfile = (desktop_dockerfile() if desktop else DOCKERFILE) if dockerfile is None else dockerfile
    prefix = "kairos-computer-desktop:" if desktop else "kairos-computer:"
    return prefix + hashlib.sha256(dockerfile.encode()).hexdigest()[:12]


async def image_ready(desktop: bool = False) -> bool:
    """Check the local image cache without building or pulling anything."""
    try:
        code, _, _ = await sandbox._docker("image", "inspect", image_tag(desktop=desktop))
        return code == 0
    except (sandbox.SandboxUnavailable, OSError):
        return False


async def _build(tag: str, desktop: bool = False) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", tag, "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise sandbox.SandboxUnavailable(f"couldn't prepare the contained computer: {e}") from e
    try:
        recipe = desktop_dockerfile() if desktop else DOCKERFILE
        out, err = await asyncio.wait_for(proc.communicate(recipe.encode()),
                                         DESKTOP_BUILD_SECONDS if desktop else BUILD_SECONDS)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise sandbox.SandboxUnavailable("couldn't prepare the contained computer: image build timed out") from e
    if proc.returncode != 0:
        lines = (err or out).decode(errors="replace").strip().splitlines()
        reason = lines[-1][:200] if lines else f"Docker build exited {proc.returncode}"
        raise sandbox.SandboxUnavailable(f"couldn't prepare the contained computer: {reason}")


async def ensure_image(desktop: bool = False) -> str:
    """Return the cached image tag, building it once if it is missing."""
    async with _desktop_lock if desktop else _lock:
        if not await image_ready(desktop=desktop):
            if desktop:
                await _build(image_tag(desktop=True), desktop=True)
            else:
                await _build(image_tag())
        return image_tag(desktop=desktop)
