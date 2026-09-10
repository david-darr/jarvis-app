"""Image generation via Pollinations.ai (David's ask 2026-09-10: image
generation for chat and Discord, free — no API key, no signup, no billing).
Confirmed live before building against it, not assumed from docs: a real
GET request against image.pollinations.ai returned a genuine 512x512 JPEG.

Anonymous tier is rate-limited to one request per 15 seconds (per
Pollinations' own API docs) — _throttle() enforces a floor between requests
process-wide, since the free tier has no per-session/per-key concept to
divide it by anyway. A burst of requests just queues and waits its turn
rather than failing.
"""
import asyncio
import os
import time
import urllib.parse
import uuid

import httpx

from core.constants import DATA_DIR

GENERATED_DIR = os.path.join(DATA_DIR, "generated_images")
GENERATED_URL_PREFIX = "/generated-images"
_BASE_URL = "https://image.pollinations.ai/prompt/"
_MIN_INTERVAL_SECONDS = 16.0  # a hair over Pollinations' documented 15s floor

_last_request_at = 0.0
_lock = asyncio.Lock()


async def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> dict:
    """Returns {"path", "filename", "url"}. Raises on a real HTTP failure —
    callers (the agent tool, Discord's own send path) turn that into a
    plain-text explanation rather than a crash, same as every other
    best-effort external call in this codebase."""
    global _last_request_at

    async with _lock:
        wait = _MIN_INTERVAL_SECONDS - (time.time() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)

        params = {"width": width, "height": height, "nologo": "true"}
        url = _BASE_URL + urllib.parse.quote(prompt) + "?" + urllib.parse.urlencode(params)

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")

        _last_request_at = time.time()

    os.makedirs(GENERATED_DIR, exist_ok=True)
    ext = "png" if "png" in content_type else "jpg"
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    path = os.path.join(GENERATED_DIR, filename)
    with open(path, "wb") as f:
        f.write(content)

    return {"path": path, "filename": filename, "url": f"{GENERATED_URL_PREFIX}/{filename}"}
