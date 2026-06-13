"""Tests for extraction/image_transform.py — rotation detection with caching."""

from __future__ import annotations

from pathlib import Path

import pytest

from transcriptor_worker.extraction.image_transform import transform_image

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_JPG = FIXTURES / "sample.jpg"


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
