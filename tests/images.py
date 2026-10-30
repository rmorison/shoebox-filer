"""Small images built in code, for tests that need real ones."""

from __future__ import annotations

import io

from PIL import Image

# The two frames differ in size, so a PDF page's size shows which frame it holds.
MPO_FIRST_FRAME_SIZE = (100, 80)
MPO_SECOND_FRAME_SIZE = (60, 40)


def make_two_frame_mpo() -> bytes:
    """Return a two-frame MPO, as some phone cameras save photos.

    Pillow reports the result's format as ``MPO``; its first frame is an
    ordinary JPEG.
    """
    first = Image.new("RGB", MPO_FIRST_FRAME_SIZE, color="red")
    second = Image.new("RGB", MPO_SECOND_FRAME_SIZE, color="blue")
    buf = io.BytesIO()
    first.save(buf, format="MPO", save_all=True, append_images=[second])
    return buf.getvalue()
