"""Image transformation hook.

Detects image rotation and corrects it to upright orientation using
multiple fallback strategies:

1. Azure Document Intelligence (prebuilt-read model) - highest priority
2. Tesseract OCR (OSD mode) - fallback if Azure unavailable
3. No-op - fallback if neither strategy is available

Before any rotation detection, EXIF orientation is applied via
``ImageOps.exif_transpose`` so that pixel data matches the intended
visual orientation.

Images with a width or height exceeding 2048 pixels are rescaled so that
the longer side becomes 2048 pixels, maintaining aspect ratio.  This
resize happens after EXIF transpose but before rotation detection.

Rotation results are cached in a ``transforms.json`` file written back to
the source storage so that subsequent runs can skip the expensive detection
step.

In addition to the resized/rotated "derived" image (used by the rest of
the pipeline for line detection etc.), this module also builds a "raw"
image: the full, pre-resize resolution, rotated by the same amount as the
derived image so both share the same final orientation, encoded as AVIF
to keep the larger pixel count from ballooning storage size.

Usage::

    transformed, raw_image, applied = transform_image(
        raw_bytes, "jpeg", transforms=cached_list
    )
    storage.write_bytes(path, transformed)
    if raw_image is not None:
        storage.write_bytes(raw_path, raw_image["bytes"])
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any

import pillow_avif  # noqa: F401 — registers the AVIF codec with Pillow
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Format/extension used for the full-resolution "raw" image upload.
RAW_IMAGE_FORMAT = "AVIF"


def _normalize_angle(angle: float) -> int:
    """Round angle to nearest 90-degree increment.

    Azure returns -180 to 180.  We round to x*90 and convert to a
    clockwise rotation amount (0, 90, 180, 270).

    Examples:
        -91.5 -> round to -90 -> rotate 90 CW
        45    -> round to 0   -> rotate 0 CW
        135   -> round to 180 -> rotate 180 CW
    """
    rounded = round(angle / 90) * 90
    # Convert to positive clockwise rotation
    rotation = int(-rounded % 360)
    return rotation


def _image_format(image_format: str) -> str:
    """Return a Pillow-compatible format name."""
    fmt = image_format.upper() if image_format else "JPEG"
    return "JPEG" if fmt == "JPG" else fmt


def _encode_image(image: Image.Image, image_format: str) -> bytes:
    """Encode an image, converting incompatible JPEG modes when needed."""
    image_to_save = image
    if _image_format(image_format) == "JPEG" and image.mode not in ("RGB", "L"):
        image_to_save = image.convert("RGB")

    try:
        buf = io.BytesIO()
        image_to_save.save(buf, format=_image_format(image_format))
        return buf.getvalue()
    finally:
        if image_to_save is not image:
            image_to_save.close()


def _try_azure_rotation(image: Image.Image, image_format: str) -> int | None:
    """Detect rotation using Azure Document Intelligence prebuilt-read model.

    Returns rotation degrees (0, 90, 180, 270) or None if unavailable.
    """
    use_azure = os.environ.get("USE_IMAGE_TRANSFORM_AZURE_AI", "").lower() == "true"
    endpoint = os.environ.get("AZURE_AI_ENDPOINT")
    key = os.environ.get("AZURE_AI_API_KEY")

    if not (use_azure and endpoint and key):
        return None

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.core.credentials import AzureKeyCredential

        # Azure's http logging is too loud.
        _azure_logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
        _azure_logger.setLevel(logging.WARNING)

        client = DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )

        # Azure accepts encoded image bytes, but keep the source decoded only once.
        image_bytes = _encode_image(image, image_format)
        poller = client.begin_analyze_document(
            "prebuilt-read",
            AnalyzeDocumentRequest(bytes_source=image_bytes),
        )
        result = poller.result()

        if result.pages and len(result.pages) > 0:
            page = result.pages[0]
            angle = getattr(page, "angle", 0) or 0
            rotation = _normalize_angle(angle)
            logger.info("Azure detected angle=%s -> rotation=%d", angle, rotation)
            return rotation

        return None

    except Exception as exc:
        logger.warning("Azure Document Intelligence rotation detection failed: %s", exc)
        return None


def _try_tesseract_rotation(image: Image.Image) -> int | None:
    """Detect rotation using Tesseract OSD mode.

    Returns rotation degrees (0, 90, 180, 270) or None if unavailable.
    """
    try:
        import pytesseract

        osd_output = pytesseract.image_to_osd(image)

        # Parse "Rotate: <degrees>" from OSD output
        for line in osd_output.splitlines():
            if line.lower().startswith("rotate:"):
                rotation = int(line.split(":")[1].strip())
                logger.info("Tesseract detected rotation=%d", rotation)
                return rotation

        return None

    except ImportError:
        logger.debug("pytesseract not installed, skipping Tesseract strategy")
        return None
    except Exception as exc:
        logger.warning("Tesseract rotation detection failed: %s", exc)
        return None


def _detect_rotation(image: Image.Image, image_format: str) -> int | None:
    """Run the rotation detection strategy cascade.

    Returns rotation degrees (0, 90, 180, 270) or None if all strategies failed.
    """
    rotation = _try_azure_rotation(image, image_format)
    if rotation is not None:
        return rotation

    rotation = _try_tesseract_rotation(image)
    if rotation is not None:
        return rotation

    return None


MAX_DIMENSION = int(os.environ.get("IMAGE_TRANSFORM_MAX_DIMENSION", "1024"))


def _load_image(image_bytes: bytes) -> Image.Image | None:
    """Decode an image once and apply its EXIF orientation."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.load()
            return ImageOps.exif_transpose(source)
    except Exception as exc:
        logger.debug("Failed to decode image: %s", exc)
        return None


def _build_raw_image(image: Image.Image, rotation: int) -> dict[str, Any] | None:
    """Build the full-resolution "raw" image for upload.

    Applies the same clockwise *rotation* used for the derived (resized)
    image so both versions share the same final orientation, then encodes
    the result as AVIF to keep file size manageable despite the higher
    resolution.

    Args:
        image: Full-resolution, EXIF-transposed image before resizing.
        rotation: Clockwise rotation in degrees (0, 90, 180, or 270).

    Returns:
        ``{"bytes": <avif bytes>, "width": <int>, "height": <int>}`` or
        ``None`` if the source bytes could not be parsed as an image.
    """
    try:
        img = image
        if rotation:
            img = img.rotate(-rotation, expand=True)
        rgb_img = img if img.mode == "RGB" else img.convert("RGB")
        try:
            width, height = rgb_img.size
            buf = io.BytesIO()
            rgb_img.save(buf, format=RAW_IMAGE_FORMAT)
            return {"bytes": buf.getvalue(), "width": width, "height": height}
        finally:
            if rgb_img is not img:
                rgb_img.close()
            if img is not image:
                img.close()
    except Exception as exc:
        logger.warning("Failed to build raw AVIF image: %s", exc)
        return None


def build_raw_image(image_bytes: bytes, rotation: int) -> dict[str, Any] | None:
    """Build the full-resolution raw AVIF image from original source bytes.

    This is a lighter-weight entry point than :func:`transform_image` for
    callers (e.g. the raw-image backfill job) that already know the target
    rotation for a page — typically read back from a cached
    ``transforms.json`` — and only need to (re)build the raw upload without
    re-running rotation detection or resize.

    Args:
        image_bytes: Original, full-resolution image bytes as read from
            source (not yet EXIF-transposed or resized).
        rotation: Clockwise rotation in degrees (0, 90, 180, or 270) to
            match a previously-derived image's final orientation.

    Returns:
        ``{"bytes": <avif bytes>, "width": <int>, "height": <int>}`` or
        ``None`` if the source bytes could not be parsed as an image.
    """
    image = _load_image(image_bytes)
    if image is None:
        return None
    try:
        return _build_raw_image(image, rotation)
    finally:
        image.close()


def transform_image(
    image_bytes: bytes,
    image_format: str,
    transforms: list[dict[str, Any]] | None = None,
) -> tuple[bytes, dict[str, Any] | None, dict[str, Any]]:
    """Detect and correct image rotation, returning the processed bytes and applied transforms.

    Args:
        image_bytes: Raw image data as returned by page extraction.
        image_format: Format hint string (e.g. ``"jpeg"``, ``"png"``).
        transforms: Optional list of cached transform dicts for this specific
            page (e.g. ``[{"rotation": 90}]``).  Ignored when
            ``FORCE_ROTATION_REDETECTION`` is set in the environment.

    Returns:
        Tuple of (transformed image bytes, raw image dict or None, applied
        transforms dict).

        The raw image dict has the form
        ``{"bytes": <avif bytes>, "width": <int>, "height": <int>}`` and
        represents the full, pre-resize resolution image rotated to match
        the derived image's orientation and encoded as AVIF.  It is
        ``None`` if the source bytes could not be parsed as an image.

        The applied transforms dict has the form
        ``{"rotation": <val>, "original_size": (w, h) | None}`` where
        ``<val>`` is 0, 90, 180, or 270.  If rotation could not be determined
        the value is ``None``.  ``original_size`` is present only when the
        image was resized because a dimension exceeded ``MAX_DIMENSION``.
    """
    # Check for cached rotation before decoding so malformed inputs preserve
    # the previously reported cached value.
    force_re = os.environ.get("FORCE_ROTATION_REDETECTION", "").lower() in ("true", "1", "yes")

    cached_rotation: int | None = None
    if not force_re and transforms:
        for t in transforms:
            if "rotation" in t:
                cached_rotation = t["rotation"]
                logger.info("Using cached rotation=%s", cached_rotation)
                break

    # Decode once. The full-resolution image is retained only until the raw
    # AVIF has been built; the derived image is at most MAX_DIMENSION pixels.
    full_image = _load_image(image_bytes)
    if full_image is None:
        return image_bytes, None, {"rotation": cached_rotation}

    derived_image = full_image
    original_size: tuple[int, int] | None = None
    width, height = full_image.size
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        original_size = (width, height)
        if width > height:
            new_width = MAX_DIMENSION
            new_height = round(height * MAX_DIMENSION / width)
        else:
            new_height = MAX_DIMENSION
            new_width = round(width * MAX_DIMENSION / height)
        derived_image = full_image.resize((new_width, new_height), Image.LANCZOS)
        logger.info("Resized image from %dx%d to %dx%d", width, height, new_width, new_height)

    # Step 4: Detect rotation if not cached
    try:
        if cached_rotation is not None:
            rotation = cached_rotation
        else:
            rotation = _detect_rotation(derived_image, image_format)

        # Build the high-quality raw AVIF before releasing the only full-size image.
        raw_image = _build_raw_image(full_image, rotation or 0)

        result_image = derived_image
        if rotation:
            result_image = derived_image.rotate(-rotation, expand=True)
        try:
            result_bytes = _encode_image(result_image, image_format)
        finally:
            if result_image is not derived_image:
                result_image.close()

        applied: dict[str, Any] = {"rotation": rotation}
        if original_size is not None:
            applied["original_size"] = original_size
        return result_bytes, raw_image, applied
    finally:
        if derived_image is not full_image:
            derived_image.close()
        full_image.close()
