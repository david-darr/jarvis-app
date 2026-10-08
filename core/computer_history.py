"""Small visual records of model actions, never of the person's input.

The registry publishes these through the current run. Only a downscaled,
bounded JPEG is kept in a run event; live and closed frames stay in memory.
"""
import base64
import io

from PIL import Image

THUMBNAIL_LIMIT = 60 * 1024


def jpeg(data: bytes, size=(1280, 800), limit: int | None = None) -> bytes | None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
        image.thumbnail(size)
        while True:
            output = io.BytesIO()
            image.save(output, "JPEG", quality=65, optimize=True)
            result = output.getvalue()
            if limit is None or len(result) <= limit:
                return result
            image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)))
    except (OSError, ValueError):
        return None


def thumbnail(data: bytes) -> str | None:
    image = jpeg(data, (960, 600), THUMBNAIL_LIMIT)
    return base64.b64encode(image).decode() if image else None
