"""Image-to-PDF conversion for receipt files."""

from __future__ import annotations

import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)

# MPO (Multi-Picture Object) is a JPEG holding more than one frame, as some
# phone cameras save photos. Only its first frame, an ordinary JPEG, is kept.
_SUPPORTED_FORMATS = {"JPEG", "MPO", "PNG"}


def image_to_pdf(image_data: bytes) -> bytes:
    """Convert image bytes (JPEG, MPO, PNG) to PDF bytes.

    An MPO converts from its first frame only.

    Args:
        image_data: Raw image file bytes.

    Returns:
        PDF file bytes containing the image.

    Raises:
        ValueError: If the image format is not supported or the data
            cannot be decoded as an image.
    """
    try:
        img = Image.open(io.BytesIO(image_data))
    except Exception as exc:
        raise ValueError("Unable to decode image data") from exc

    fmt = img.format
    if fmt not in _SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported image format {fmt!r}; expected one of {_SUPPORTED_FORMATS}"
        )

    if fmt == "MPO":
        # Saving without save_all writes the current frame: make it the first.
        img.seek(0)

    # PDF does not support alpha channel — convert RGBA to RGB.
    output_img: Image.Image = img
    if img.mode in ("RGBA", "LA", "PA"):
        logger.debug("Converting %s image from %s to RGB", fmt, img.mode)
        output_img = img.convert("RGB")

    buf = io.BytesIO()
    output_img.save(buf, format="PDF")
    return buf.getvalue()
