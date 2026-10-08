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


def image_tag(dockerfile: str | None = None) -> str:
    dockerfile = DOCKERFILE if dockerfile is None else dockerfile
    return "kairos-computer:" + hashlib.sha256(dockerfile.encode()).hexdigest()[:12]


async def image_ready() -> bool:
    """Check the local image cache without building or pulling anything."""
    try:
        code, _, _ = await sandbox._docker("image", "inspect", image_tag())
        return code == 0
    except (sandbox.SandboxUnavailable, OSError):
        return False


async def _build(tag: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", tag, "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise sandbox.SandboxUnavailable(f"couldn't prepare the contained computer: {e}") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(DOCKERFILE.encode()), BUILD_SECONDS)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise sandbox.SandboxUnavailable("couldn't prepare the contained computer: image build timed out") from e
    if proc.returncode != 0:
        lines = (err or out).decode(errors="replace").strip().splitlines()
        reason = lines[-1][:200] if lines else f"Docker build exited {proc.returncode}"
        raise sandbox.SandboxUnavailable(f"couldn't prepare the contained computer: {reason}")


async def ensure_image() -> str:
    """Return the cached image tag, building it once if it is missing."""
    async with _lock:
        if not await image_ready():
            await _build(image_tag())
        return image_tag()
