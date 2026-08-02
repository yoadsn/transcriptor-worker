"""Page extraction — PDF rasterization and image conversion.

For each submission:
- PDF files are opened with pyMuPDF and each page is rasterized to a JPEG.
- Image files (JPEG, PNG, GIF, WebP, BMP, TIFF, …) are decoded via Pillow
  and re-encoded to JPEG for a uniform output format.
- Every extracted page image passes through :func:`transform_image` before
  being written to target storage and the local temp directory.
- The full, pre-resize resolution image (rotated to match the derived
  image's orientation) is additionally uploaded to target storage as AVIF
  so the original image quality/size is not lost.

Naming convention:
    ``{doc_basename}_p{N}.jpg``    (derived/resized image; N is 1-based)
    ``{doc_basename}_p{N}.avif``  (raw, full-resolution image)

For image files *doc_basename* is the stored filename stem and N is always 1.
For PDF files *doc_basename* is the PDF stem and N ranges over all pages.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image as PILImage

from transcriptor_worker.extraction.image_transform import transform_image
from transcriptor_worker.models import DescJson, DocFile, PageRecord, Submission
from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# DPI used when rasterising PDF pages. 200 DPI preserves a high-quality raw
# AVIF while substantially reducing the transient rasterization memory peak.
PDF_RENDER_DPI = 200

# Output format for all page images written to storage.
_OUTPUT_FORMAT = "jpeg"
_OUTPUT_EXT = ".jpg"

# Output format/extension for the full-resolution raw image upload.
_RAW_OUTPUT_EXT = ".avif"


@dataclass
class ExtractPagesResult:
    """Return value from :func:`extract_pages`."""

    page_records: list[PageRecord] = field(default_factory=list)
    transforms: dict[str, Any] = field(default_factory=dict)


def _is_pdf(doc_file: DocFile) -> bool:
    """Return True if *doc_file* should be treated as a PDF."""
    ext = doc_file.file_extension.lower()
    if ext == ".pdf":
        return True
    # Fall back to MIME type if extension is absent/unknown
    mime = (doc_file.mime_type or "").lower()
    return mime == "application/pdf"


def _page_filename(doc_basename: str, page_number: int) -> str:
    """Derive the output filename for a page image (1-based *page_number*)."""
    return f"{doc_basename}_p{page_number}{_OUTPUT_EXT}"


def _raw_page_filename(doc_basename: str, page_number: int) -> str:
    """Derive the output filename for a raw (full-resolution) page image."""
    return f"{doc_basename}_p{page_number}{_RAW_OUTPUT_EXT}"


def _write_page(
    image_bytes: bytes,
    image_format: str,
    submission_id: str,
    filename: str,
    raw_filename: str,
    target_storage: StorageBackend,
    temp_dir: str,
    transforms: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Transform, then write page image (and raw original) to storage.

    The derived (resized/rotated) image is written to both target storage
    and the local temp dir (needed by downstream line detection).  The raw,
    full-resolution image is written only to target storage as AVIF.

    Returns:
        Tuple of (filename, raw_info dict or None, applied_transforms dict).
        ``raw_info`` has the form
        ``{"filename": str, "width": int, "height": int}`` and is ``None``
        if the raw image could not be built.
    """
    data, raw_image, applied = transform_image(image_bytes, image_format, transforms=transforms)

    target_path = f"{submission_id}/{filename}"
    target_storage.write_bytes(target_path, data)
    logger.debug("Wrote page to target: %s", target_path)

    local_dir = Path(temp_dir) / submission_id
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / filename
    local_path.write_bytes(data)
    logger.debug("Wrote page to temp: %s", local_path)

    raw_info: dict[str, Any] | None = None
    if raw_image is not None:
        raw_target_path = f"{submission_id}/{raw_filename}"
        target_storage.write_bytes(raw_target_path, raw_image["bytes"])
        logger.debug("Wrote raw page to target: %s", raw_target_path)
        raw_info = {
            "filename": raw_filename,
            "width": raw_image["width"],
            "height": raw_image["height"],
        }

    return filename, raw_info, applied


def _extract_pdf_pages(
    doc_file: DocFile,
    raw_bytes: bytes,
    submission_id: str,
    target_storage: StorageBackend,
    temp_dir: str,
    transforms_cache: dict[str, Any] | None = None,
) -> tuple[list[PageRecord], dict[str, Any]]:
    """Rasterize each page of a PDF and write page images.

    Returns one :class:`PageRecord` per page and a transforms dict.
    On per-page failure the record gets ``status="failed"`` and processing
    continues with the next page.
    """
    doc_basename = Path(doc_file.stored_filename).stem
    records: list[PageRecord] = []
    applied_transforms: dict[str, Any] = {}

    try:
        pdf = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception as exc:
        logger.error("Failed to open PDF %s: %s", doc_file.stored_filename, exc)
        return (
            [
                PageRecord(
                    submission_id=submission_id,
                    doc_filename=doc_file.stored_filename,
                    page_number=1,
                    status="failed",
                    error=f"Failed to open PDF: {exc}",
                )
            ],
            applied_transforms,
        )

    logger.info(
        "Extracting %d page(s) from PDF %s", pdf.page_count, doc_file.stored_filename
    )

    for page_idx in range(pdf.page_count):
        page_number = page_idx + 1
        filename = _page_filename(doc_basename, page_number)
        raw_filename = _raw_page_filename(doc_basename, page_number)
        try:
            # Log before AND after the (pure-CPU) rasterization so the logs
            # unambiguously show whether a hang is at PDF rendering (`page.get_pixmap`)
            # or further on (transform / S3 write).
            logger.info(
                "Rendering page %d/%d of %s…",
                page_number,
                pdf.page_count,
                doc_file.stored_filename,
            )
            page = pdf[page_idx]
            pixmap = page.get_pixmap(dpi=PDF_RENDER_DPI)
            image_bytes = pixmap.tobytes(_OUTPUT_FORMAT)
            width, height = pixmap.width, pixmap.height
            # The JPEG bytes are independent of the pixmap; release the large
            # uncompressed buffer before Pillow performs further processing.
            del pixmap
            logger.info(
                "Rendered page %d/%d of %s: %dx%d => %d bytes",
                page_number,
                pdf.page_count,
                doc_file.stored_filename,
                width,
                height,
                len(image_bytes),
            )

            # Push down only the relevant transforms for this page
            page_transforms: list[dict[str, Any]] | None = None
            if transforms_cache:
                doc_entry = transforms_cache.get(doc_file.stored_filename, {})
                page_transforms = doc_entry.get(str(page_number))

            _, raw_info, applied = _write_page(
                image_bytes,
                _OUTPUT_FORMAT,
                submission_id,
                filename,
                raw_filename,
                target_storage,
                temp_dir,
                transforms=page_transforms,
            )

            # Record the applied transform for this page
            page_key = str(page_number)
            applied_transforms[page_key] = [applied]

            records.append(
                PageRecord(
                    submission_id=submission_id,
                    doc_filename=doc_file.stored_filename,
                    page_number=page_number,
                    status="pending",
                    image_filename=filename,
                    raw_image_filename=raw_info["filename"] if raw_info else "",
                    raw_image_width=raw_info["width"] if raw_info else 0,
                    raw_image_height=raw_info["height"] if raw_info else 0,
                )
            )
            logger.debug(
                "Extracted page %d/%d of %s",
                page_number,
                pdf.page_count,
                doc_file.stored_filename,
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to extract page %d from %s: %s",
                page_number,
                doc_file.stored_filename,
                exc,
            )
            records.append(
                PageRecord(
                    submission_id=submission_id,
                    doc_filename=doc_file.stored_filename,
                    page_number=page_number,
                    status="failed",
                    error=str(exc),
                )
            )

    pdf.close()
    return records, applied_transforms


def _extract_image_page(
    doc_file: DocFile,
    raw_bytes: bytes,
    submission_id: str,
    target_storage: StorageBackend,
    temp_dir: str,
    transforms_cache: dict[str, Any] | None = None,
) -> tuple[PageRecord, dict[str, Any]]:
    """Convert any supported image to a single JPEG page.

    Accepts common image formats (JPEG, PNG, GIF, WebP, BMP, TIFF, etc.)
    via Pillow and re-encodes to JPEG for a uniform output format.
    Returns a single :class:`PageRecord` and applied transforms dict.
    """
    doc_basename = Path(doc_file.stored_filename).stem
    filename = _page_filename(doc_basename, 1)
    raw_filename = _raw_page_filename(doc_basename, 1)

    try:
        image = PILImage.open(io.BytesIO(raw_bytes))
        exif_data = image.info.get("exif")
        image = image.convert("RGB")
        buf = io.BytesIO()
        save_kwargs: dict[str, Any] = {"format": "JPEG"}
        if exif_data:
            save_kwargs["exif"] = exif_data
        image.save(buf, **save_kwargs)
        image_bytes = buf.getvalue()

        # Push down only the relevant transforms for this page
        page_transforms: list[dict[str, Any]] | None = None
        if transforms_cache:
            doc_entry = transforms_cache.get(doc_file.stored_filename, {})
            page_transforms = doc_entry.get("1")

        _, raw_info, applied = _write_page(
            image_bytes,
            _OUTPUT_FORMAT,
            submission_id,
            filename,
            raw_filename,
            target_storage,
            temp_dir,
            transforms=page_transforms,
        )

        logger.debug("Converted image %s → %s", doc_file.stored_filename, filename)
        return (
            PageRecord(
                submission_id=submission_id,
                doc_filename=doc_file.stored_filename,
                page_number=1,
                status="pending",
                image_filename=filename,
                raw_image_filename=raw_info["filename"] if raw_info else "",
                raw_image_width=raw_info["width"] if raw_info else 0,
                raw_image_height=raw_info["height"] if raw_info else 0,
            ),
            {"1": [applied]},
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to process image %s: %s", doc_file.stored_filename, exc)
        return (
            PageRecord(
                submission_id=submission_id,
                doc_filename=doc_file.stored_filename,
                page_number=1,
                status="failed",
                error=str(exc),
            ),
            {},
        )


def extract_pages(
    submission: Submission,
    source_storage: StorageBackend,
    target_storage: StorageBackend,
    temp_dir: str,
) -> ExtractPagesResult:
    """Extract all page images for *submission*.

    Reads ``desc.json`` from source storage, then processes each listed doc:
    - PDFs are rasterized page-by-page with pyMuPDF at :data:`PDF_RENDER_DPI`.
    - Image files are passed through (re-encoded to JPEG).
    - Every page image is run through :func:`transform_image` (rotation correction).
    - Pages are written to both target storage and local temp storage.

    If a ``transforms.json`` file exists alongside ``desc.json`` it is loaded
    and used to skip rotation detection for pages that were already processed.
    After extraction, all applied transforms are collected and returned.

    On per-doc failure a :class:`PageRecord` with ``status="failed"`` is
    appended and processing continues with the remaining docs.

    Args:
        submission: The submission to process.
        source_storage: Storage backend containing source files.
        target_storage: Storage backend for output page images.
        temp_dir: Root of local temp directory for this run.

    Returns:
        :class:`ExtractPagesResult` containing page records and applied transforms.
    """
    desc_path = f"{submission.source_path}/desc.json"
    logger.info("Processing submission %s from %s", submission.id, desc_path)

    try:
        raw_json = source_storage.read_text(desc_path)
    except Exception as exc:
        logger.error("Cannot read desc.json for %s: %s", submission.id, exc)
        return ExtractPagesResult(
            page_records=[
                PageRecord(
                    submission_id=submission.id,
                    doc_filename="desc.json",
                    page_number=1,
                    status="failed",
                    error=f"Cannot read desc.json: {exc}",
                )
            ]
        )

    try:
        desc = DescJson.from_dict(json.loads(raw_json))
    except Exception as exc:
        logger.error("Cannot parse desc.json for %s: %s", submission.id, exc)
        return ExtractPagesResult(
            page_records=[
                PageRecord(
                    submission_id=submission.id,
                    doc_filename="desc.json",
                    page_number=1,
                    status="failed",
                    error=f"Cannot parse desc.json: {exc}",
                )
            ]
        )

    # Load cached transforms if available
    transforms_path = f"{submission.source_path}/transforms.json"
    transforms_cache: dict[str, Any] | None = None
    try:
        transforms_json = source_storage.read_text(transforms_path)
        transforms_cache = json.loads(transforms_json)
        logger.info("Loaded cached transforms for submission %s", submission.id)
    except Exception:
        logger.debug("No cached transforms found for submission %s", submission.id)
        transforms_cache = None

    all_records: list[PageRecord] = []
    all_transforms: dict[str, Any] = {}

    for doc_file in desc.files:
        doc_path = f"{submission.source_path}/{doc_file.stored_filename}"
        logger.info(
            "Reading doc %s for submission %s", doc_file.stored_filename, submission.id
        )

        try:
            raw_bytes = source_storage.read_bytes(doc_path)
        except Exception as exc:
            logger.warning(
                "Cannot read doc %s for submission %s: %s",
                doc_file.stored_filename,
                submission.id,
                exc,
            )
            all_records.append(
                PageRecord(
                    submission_id=submission.id,
                    doc_filename=doc_file.stored_filename,
                    page_number=1,
                    status="failed",
                    error=f"Cannot read file: {exc}",
                )
            )
            continue

        if _is_pdf(doc_file):
            records, doc_transforms = _extract_pdf_pages(
                doc_file, raw_bytes, submission.id, target_storage, temp_dir,
                transforms_cache=transforms_cache,
            )
        else:
            record, doc_transforms = _extract_image_page(
                doc_file, raw_bytes, submission.id, target_storage, temp_dir,
                transforms_cache=transforms_cache,
            )
            records = [record]

        if doc_transforms:
            all_transforms[doc_file.stored_filename] = doc_transforms

        all_records.extend(records)

    logger.info(
        "Submission %s: extracted %d page(s) (%d failed)",
        submission.id,
        len(all_records),
        sum(1 for r in all_records if r.status == "failed"),
    )
    return ExtractPagesResult(page_records=all_records, transforms=all_transforms)
