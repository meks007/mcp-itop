"""
attachment_store/image.py - Pillow-based image normalization.

Converts any image format to JPEG and compresses/downscales to fit within
IMAGE_MAX_BYTES. No SQLite dependency.
"""

from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image as _PILImage

from config import IMAGE_JPEG_QUALITY, IMAGE_MAX_BYTES

logger = logging.getLogger(__name__)

# Quality steps tried in order before falling back to downscaling.
_JPEG_QUALITY_STEPS = (75, 60, 45, 30)

# Minimum scale factor; below this we stop downscaling.
_MIN_SCALE = 0.10


def _normalize_image(
    data: bytes,
    mimetype: str,
    filename: str,
) -> tuple[bytes, str, str]:
    """Convert any image to JPEG and compress/downscale to fit IMAGE_MAX_BYTES.

    Strategy:
      1. Open with Pillow (supports JPEG, BMP, TIFF, WebP, GIF, ICO, PNG ...).
      2. Flatten alpha channel onto a white background (JPEG has no alpha).
      3. Try encoding at IMAGE_JPEG_QUALITY. If the result exceeds IMAGE_MAX_BYTES,
         retry at each quality step in _JPEG_QUALITY_STEPS (75, 60, 45, 30).
      4. If still too large after all quality steps, downscale by 75% per
         iteration at minimum quality until the image fits or drops below
         _MIN_SCALE.
      5. Rename the file extension to .jpg.

    Falls back to the original data and mimetype on any Pillow error so that
    a broken image does not block the whole request.
    IMAGE_MAX_BYTES <= 0 disables size capping (single encode at base quality).
    """
    if not data:
        return data, mimetype, filename

    max_bytes = IMAGE_MAX_BYTES
    base_quality = IMAGE_JPEG_QUALITY

    try:
        img = _PILImage.open(BytesIO(data))

        # Flatten alpha onto white so JPEG encoding does not error.
        if img.mode in ("RGBA", "LA", "PA"):
            bg = _PILImage.new("RGB", img.size, (255, 255, 255))
            if img.mode == "PA":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        new_filename = stem + ".jpg"

        def _encode(frame: _PILImage.Image, quality: int) -> bytes:
            buf = BytesIO()
            frame.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()

        # Step 1: try quality ladder at full resolution.
        result = _encode(img, base_quality)
        if max_bytes <= 0 or len(result) <= max_bytes:
            logger.debug(
                "[attachment_store] _normalize_image: %s -> JPEG %d bytes"
                " quality=%d (original %d bytes)",
                filename, len(result), base_quality, len(data),
            )
            return result, "image/jpeg", new_filename

        used_quality = base_quality
        for q in _JPEG_QUALITY_STEPS:
            result = _encode(img, q)
            used_quality = q
            if len(result) <= max_bytes:
                break

        if len(result) <= max_bytes:
            logger.debug(
                "[attachment_store] _normalize_image: %s -> JPEG %d bytes"
                " quality=%d after quality reduction (original %d bytes)",
                filename, len(result), used_quality, len(data),
            )
            return result, "image/jpeg", new_filename

        # Step 2: downscale at minimum quality until it fits.
        scale = 0.75
        while scale >= _MIN_SCALE:
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            resized = img.resize((new_w, new_h), _PILImage.LANCZOS)
            result = _encode(resized, _JPEG_QUALITY_STEPS[-1])
            if len(result) <= max_bytes:
                break
            scale *= 0.75

        logger.debug(
            "[attachment_store] _normalize_image: %s -> JPEG %d bytes"
            " quality=%d scale=%.2f (original %d bytes)",
            filename, len(result), _JPEG_QUALITY_STEPS[-1], scale, len(data),
        )
        return result, "image/jpeg", new_filename

    except Exception as exc:
        logger.warning(
            "[attachment_store] _normalize_image: failed for %s, keeping original: %s",
            filename, exc,
        )
        return data, mimetype, filename
