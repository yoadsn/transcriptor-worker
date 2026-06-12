"""Tests for extraction/image_transform.py — no-op pass-through."""

from __future__ import annotations

from pathlib import Path

import pytest

from transcriptor_worker.extraction.image_transform import transform_image

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_JPG = FIXTURES / "sample.jpg"


class TestTransformImage:
    def test_returns_identical_bytes_for_jpeg(self):
        data = SAMPLE_JPG.read_bytes()
        result = transform_image(data, "jpeg")
        assert result is data, "Expected the exact same bytes object (no-op)"

    def test_returns_identical_bytes_for_png(self):
        data = b"\x89PNG\r\n\x1a\nfake_png_payload"
        result = transform_image(data, "png")
        assert result is data

    def test_returns_identical_bytes_for_arbitrary_format(self):
        data = b"binary blob"
        result = transform_image(data, "tiff")
        assert result is data

    def test_empty_bytes_passed_through(self):
        data = b""
        result = transform_image(data, "jpeg")
        assert result is data

    def test_large_payload_passed_through(self):
        data = bytes(range(256)) * 1000
        result = transform_image(data, "jpeg")
        assert result is data

    def test_does_not_modify_bytes_content(self):
        data = b"\x00\x01\x02\x03"
        result = transform_image(data, "jpeg")
        assert result == data
