"""Tests for receipt_index.image_converter."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from receipt_index.image_converter import image_to_pdf


def _make_image(
    fmt: str,
    mode: str = "RGB",
    size: tuple[int, int] = (100, 80),
) -> bytes:
    """Create minimal image bytes for testing."""
    img = Image.new(mode, size, color="red")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


class TestImageToPdf:
    """Tests for the image_to_pdf function."""

    def test_jpeg_produces_valid_pdf(self) -> None:
        jpeg_data = _make_image("JPEG")
        result = image_to_pdf(jpeg_data)
        assert result[:5] == b"%PDF-"

    def test_png_produces_valid_pdf(self) -> None:
        png_data = _make_image("PNG")
        result = image_to_pdf(png_data)
        assert result[:5] == b"%PDF-"

    def test_rgba_png_converts_to_rgb(self) -> None:
        rgba_data = _make_image("PNG", mode="RGBA")
        result = image_to_pdf(rgba_data)
        assert result[:5] == b"%PDF-"

    def test_la_mode_converts_to_rgb(self) -> None:
        la_data = _make_image("PNG", mode="LA")
        result = image_to_pdf(la_data)
        assert result[:5] == b"%PDF-"

    def test_unsupported_format_raises_value_error(self) -> None:
        bmp_data = _make_image("BMP")
        with pytest.raises(ValueError, match="Unsupported image format"):
            image_to_pdf(bmp_data)

    def test_invalid_data_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unable to decode image data"):
            image_to_pdf(b"not-an-image")

    def test_empty_data_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unable to decode image data"):
            image_to_pdf(b"")
