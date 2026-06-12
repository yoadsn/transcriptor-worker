"""Page extraction — PDF rasterization and image pass-through.

For each submission:
- PDF files are opened with pyMuPDF and each page is rasterized to a JPEG.
- Image files are passed through as-is (re-encoded to JPEG for uniformity).
- Every extracted page image passes through :func:`transform_image` before
  being written to target storage and the local temp directory.

Naming convention:
    ``{doc_basename}_p{N}.jpg``   (N is 1-based page number)

For image files *doc_basename* is the stored filename stem and N is always 1.
For PDF files *doc_basename* is the PDF stem and N ranges over all pages.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path, PurePosixPath

import pymupdf

from transcriptor_worker.extraction.image_transform import transform_image
from transcriptor_worker.models import DescJson, DocFile, PageRecord, Submission
from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# DPI used when rasterising PDF pages.
PDF_RENDER_DPI = 300

# Image file extensions treated as direct pass-through (lower-case, with dot).
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}

# Output format for all page images written to storage.
_OUTPUT_FORMAT = "jpeg"
_OUTPUT_EXT = ".jpg"


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


def _write_page(
    image_bytes: bytes,
    image_format: str,
    submission_id: str,
    filename: str,
    target_storage: StorageBackend,
    temp_dir: str,
) -> str:
    """Transform, then write page image to both target storage and temp dir.

    Returns:
        The filename (not full path) of the written image.
    """
    data = transform_image(image_bytes, image_format)

    target_path = f"{submission_id}/{filename}"
    target_storage.write_bytes(target_path, data)
    logger.debug("Wrote page to target: %s", target_path)

    local_dir = Path(temp_dir) / submission_id
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / filename
    local_path.write_bytes(data)
    logger.debug("Wrote page to temp: %s", local_path)

    return filename


def _extract_pdf_pages(
    doc_file: DocFile,
    raw_bytes: bytes,
    submission_id: str,
    target_storage: StorageBackend,
    temp_dir: str,
) -> list[PageRecord]:
    """Rasterize each page of a PDF and write page images.

    Returns one :class:`PageRecord` per page.  On per-page failure the record
    gets ``status="failed"`` and processing continues with the next page.
    """
    doc_basename = Path(doc_file.stored_filename).stem
    records: list[PageRecord] = []

    try:
        pdf = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception as exc:
        logger.error(
            "Failed to open PDF %s: %s", doc_file.stored_filename, exc
        )
        return [
            PageRecord(
                submission_id=submission_id,
                doc_filename=doc_file.stored_filename,
                page_number=1,
                status="failed",
                error=f"Failed to open PDF: {exc}",
            )
        ]

    logger.info(
        "Extracting %d page(s) from PDF %s", pdf.page_count, doc_file.stored_filename
    )

    for page_idx in range(pdf.page_count):
        page_number = page_idx + 1
        filename = _page_filename(doc_basename, page_number)
        try:
            page = pdf[page_idx]
            pixmap = page.get_pixmap(dpi=PDF_RENDER_DPI)
            image_bytes = pixmap.tobytes(_OUTPUT_FORMAT)

            _write_page(
                image_bytes,
                _OUTPUT_FORMAT,
                submission_id,
                filename,
                target_storage,
                temp_dir,
            )

            records.append(
                PageRecord(
                    submission_id=submission_id,
                    doc_filename=doc_file.stored_filename,
                    page_number=page_number,
                    status="pending",
                    image_filename=filename,
                )
            )
            logger.debug("Extracted page %d/%d of %s", page_number, pdf.page_count, doc_file.stored_filename)

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
    return records


def _extract_image_page(
    doc_file: DocFile,
    raw_bytes: bytes,
    submission_id: str,
    target_storage: StorageBackend,
    temp_dir: str,
) -> PageRecord:
    """Pass an image file through as a single page.

    The image is re-encoded to JPEG for a uniform output format.
    Returns a single :class:`PageRecord`.
    """
    doc_basename = Path(doc_file.stored_filename).stem
    filename = _page_filename(doc_basename, 1)

    try:
        # Re-encode to JPEG via pyMuPDF Pixmap for consistent output format.
        # This handles JPEG, PNG, TIFF, BMP, WebP, etc.
        pixmap = pymupdf.Pixmap(raw_bytes)
        # Convert CMYK/alpha to RGB if necessary before JPEG encoding
        if pixmap.colorspace and pixmap.colorspace.n > 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        elif pixmap.alpha:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        image_bytes = pixmap.tobytes(_OUTPUT_FORMAT)

        _write_page(
            image_bytes,
            _OUTPUT_FORMAT,
            submission_id,
            filename,
            target_storage,
            temp_dir,
        )

        logger.debug("Passed through image %s as %s", doc_file.stored_filename, filename)
        return PageRecord(
            submission_id=submission_id,
            doc_filename=doc_file.stored_filename,
            page_number=1,
            status="pending",
            image_filename=filename,
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to process image %s: %s", doc_file.stored_filename, exc
        )
        return PageRecord(
            submission_id=submission_id,
            doc_filename=doc_file.stored_filename,
            page_number=1,
            status="failed",
            error=str(exc),
        )


def extract_pages(
    submission: Submission,
    source_storage: StorageBackend,
    target_storage: StorageBackend,
    temp_dir: str,
) -> list[PageRecord]:
    """Extract all page images for *submission*.

    Reads ``desc.json`` from source storage, then processes each listed doc:
    - PDFs are rasterized page-by-page with pyMuPDF at :data:`PDF_RENDER_DPI`.
    - Image files are passed through (re-encoded to JPEG).
    - Every page image is run through :func:`transform_image` (no-op today).
    - Pages are written to both target storage and local temp storage.

    On per-doc failure a :class:`PageRecord` with ``status="failed"`` is
    appended and processing continues with the remaining docs.

    Args:
        submission: The submission to process.
        source_storage: Storage backend containing source files.
        target_storage: Storage backend for output page images.
        temp_dir: Root of local temp directory for this run.

    Returns:
        List of :class:`PageRecord`, one per extracted page (or failed attempt).
    """
    desc_path = f"{submission.source_path}/desc.json"
    logger.info("Processing submission %s from %s", submission.id, desc_path)

    try:
        raw_json = source_storage.read_text(desc_path)
    except Exception as exc:
        logger.error("Cannot read desc.json for %s: %s", submission.id, exc)
        return [
            PageRecord(
                submission_id=submission.id,
                doc_filename="desc.json",
                page_number=1,
                status="failed",
                error=f"Cannot read desc.json: {exc}",
            )
        ]

    try:
        import json as _json
        desc = DescJson.from_dict(_json.loads(raw_json))
    except Exception as exc:
        logger.error("Cannot parse desc.json for %s: %s", submission.id, exc)
        return [
            PageRecord(
                submission_id=submission.id,
                doc_filename="desc.json",
                page_number=1,
                status="failed",
                error=f"Cannot parse desc.json: {exc}",
            )
        ]

    all_records: list[PageRecord] = []

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
            records = _extract_pdf_pages(
                doc_file, raw_bytes, submission.id, target_storage, temp_dir
            )
        else:
            records = [
                _extract_image_page(
                    doc_file, raw_bytes, submission.id, target_storage, temp_dir
                )
            ]

        all_records.extend(records)

    logger.info(
        "Submission %s: extracted %d page(s) (%d failed)",
        submission.id,
        len(all_records),
        sum(1 for r in all_records if r.status == "failed"),
    )
    return all_records
