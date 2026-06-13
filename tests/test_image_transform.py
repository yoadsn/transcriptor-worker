"""Tests for extraction/image_transform.py — rotation detection with caching."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from transcriptor_worker.extraction.image_transform import MAX_DIMENSION, transform_image

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_JPG = FIXTURES / "sample.jpg"


def _create_jpeg_bytes(width: int, height: int) -> bytes:
    """Create a minimal JPEG image with the given dimensions."""
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class TestTransformImage:
    def test_returns_bytes_and_applied_for_jpeg(self):
        data = SAMPLE_JPG.read_bytes()
        result, applied = transform_image(data, "jpeg")
        assert isinstance(result, bytes)
        assert isinstance(applied, dict)
        assert "rotation" in applied

    def test_returns_bytes_and_applied_for_png(self):
        data = b"\x89PNG\r\n\x1a\nfake_png_payload"
        result, applied = transform_image(data, "png")
        assert isinstance(result, bytes)
        assert isinstance(applied, dict)
        assert "rotation" in applied

    def test_returns_bytes_and_applied_for_arbitrary_format(self):
        data = b"binary blob"
        result, applied = transform_image(data, "tiff")
        assert isinstance(result, bytes)
        assert isinstance(applied, dict)
        assert "rotation" in applied

    def test_empty_bytes_passed_through(self):
        data = b""
        result, applied = transform_image(data, "jpeg")
        assert result == data
        assert applied["rotation"] is None

    def test_large_payload_passed_through(self):
        data = bytes(range(256)) * 1000
        result, applied = transform_image(data, "jpeg")
        assert isinstance(result, bytes)
        assert applied["rotation"] is None

    def test_does_not_modify_bytes_content(self):
        data = b"\x00\x01\x02\x03"
        result, applied = transform_image(data, "jpeg")
        assert result == data
        assert applied["rotation"] is None

    def test_uses_cached_rotation(self):
        data = SAMPLE_JPG.read_bytes()
        cached = [{"rotation": 90}]
        result, applied = transform_image(data, "jpeg", transforms=cached)
        assert applied["rotation"] == 90

    def test_skips_detection_when_cached(self):
        """Cached rotation should be used even if detection would fail."""
        data = b"not an image"
        cached = [{"rotation": 180}]
        result, applied = transform_image(data, "jpeg", transforms=cached)
        assert applied["rotation"] == 180

    def test_cached_rotation_zero(self):
        data = SAMPLE_JPG.read_bytes()
        cached = [{"rotation": 0}]
        result, applied = transform_image(data, "jpeg", transforms=cached)
        assert applied["rotation"] == 0

    def test_empty_transforms_list_triggers_detection(self):
        data = SAMPLE_JPG.read_bytes()
        result, applied = transform_image(data, "jpeg", transforms=[])
        assert applied["rotation"] is None


class TestResizeTransform:
    def test_wide_image_is_resized(self):
        data = _create_jpeg_bytes(4000, 2000)
        result, applied = transform_image(data, "jpeg")
        assert applied["original_size"] == (4000, 2000)
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width == MAX_DIMENSION
        assert result_img.height == 1500

    def test_tall_image_is_resized(self):
        data = _create_jpeg_bytes(2000, 5000)
        result, applied = transform_image(data, "jpeg")
        assert applied["original_size"] == (2000, 5000)
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width == 1200
        assert result_img.height == MAX_DIMENSION

    def test_square_image_over_limit_is_resized(self):
        data = _create_jpeg_bytes(4000, 4000)
        result, applied = transform_image(data, "jpeg")
        assert applied["original_size"] == (4000, 4000)
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width == MAX_DIMENSION
        assert result_img.height == MAX_DIMENSION

    def test_small_image_is_not_resized(self):
        data = _create_jpeg_bytes(1000, 800)
        result, applied = transform_image(data, "jpeg")
        assert "original_size" not in applied
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width == 1000
        assert result_img.height == 800

    def test_image_at_exact_limit_is_not_resized(self):
        data = _create_jpeg_bytes(MAX_DIMENSION, MAX_DIMENSION)
        result, applied = transform_image(data, "jpeg")
        assert "original_size" not in applied
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width == MAX_DIMENSION
        assert result_img.height == MAX_DIMENSION

    def test_aspect_ratio_is_preserved(self):
        data = _create_jpeg_bytes(6000, 3000)
        result, applied = transform_image(data, "jpeg")
        result_img = Image.open(io.BytesIO(result))
        assert result_img.width / result_img.height == pytest.approx(2.0, rel=1e-2)
