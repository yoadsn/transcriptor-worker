"""Image transformation hook.

Today this is a no-op pass-through.  It sits between page extraction (PDF
rasterization or raw image read) and writing to storage, providing the
insertion point for future per-image processing such as:

- Deskewing (correct rotation from scanning)
- Binarization (convert to black-and-white for cleaner OCR input)
- Contrast enhancement
- Resampling to a canonical DPI / resolution

Usage::

    transformed = transform_image(raw_bytes, "jpeg")
    storage.write_bytes(path, transformed)
"""

from __future__ import annotations


def transform_image(image_bytes: bytes, image_format: str) -> bytes:  # noqa: ARG001
    """Apply image transformations and return the processed bytes.

    Args:
        image_bytes: Raw image data as returned by page extraction.
        image_format: Format hint string (e.g. ``"jpeg"``, ``"png"``).
            Currently unused; reserved for format-aware future transforms.

    Returns:
        Transformed image bytes.  Today this is identical to *image_bytes*.
    """
    # No-op: return input unchanged.
    # Future transforms (Pillow-based deskew, binarization, contrast, etc.)
    # should be applied here before returning.
    return image_bytes
