"""Backfill raw (full-resolution) AVIF images for already-completed pages.

Activated via ``BACKFILL_RAW_IMAGES=true``. Unlike a normal pipeline run,
this mode:

- Does **not** discover or process any submission that is not already
  present in the target's ``pages.csv``. No new source submissions are
  processed and no ``metadata.json`` / ``lines.json`` files are created
  from scratch — only pages that are already ``status="completed"`` in
  ``pages.csv`` and are missing ``raw_image_filename`` are touched.
- Does **not** run Surya line detection. Existing ``lines.json`` files are
  patched in place with the new ``raw_image_filename`` / ``raw_image_width``
  / ``raw_image_height`` fields, reusing the lines that were already
  detected in the original run.
- Re-reads the *original* document bytes from source storage. This is
  required to recover the pre-resize resolution — the derived ``.jpg``
  already written to target storage is not sufficient since it has already
  been downsized. The rotation applied is the one cached in the source's
  ``transforms.json`` (from the original run) — rotation is never
  re-detected.

For each page missing a raw image:
    1. Re-rasterize (PDF) or re-convert (image) the original doc to obtain
       full-resolution bytes, exactly as the original extraction did.
    2. Look up the previously-detected/cached rotation for that page.
    3. Build & upload the raw AVIF image to target storage.
    4. Patch the page's ``lines.json`` (if present) with the new fields.
    5. Update the page's row and rewrite ``pages.csv``.
"""

from __future__ import annotations

import io
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image as PILImage

from transcriptor_worker.config import Config
from transcriptor_worker.discovery import discover_submissions
from transcriptor_worker.extraction.image_transform import build_raw_image
from transcriptor_worker.extraction.pages import (
    _OUTPUT_FORMAT,
    PDF_RENDER_DPI,
    _is_pdf,
    _raw_page_filename,
)
from transcriptor_worker.manifest import load_pages_csv, save_pages_csv
from transcriptor_worker.models import DescJson, DocFile, PageRecord
from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — cached rotation lookup
# ---------------------------------------------------------------------------


def _load_transforms(source_storage: StorageBackend, source_path: str) -> dict[str, Any]:
    """Load the cached ``transforms.json`` for a submission, or ``{}``."""
    transforms_path = f"{source_path}/transforms.json"
    try:
        return json.loads(source_storage.read_text(transforms_path))
    except Exception:
        return {}


def _cached_rotation(
    transforms_cache: dict[str, Any], doc_filename: str, page_number: int
) -> int | None:
    """Look up the cached rotation for one page, mirroring pages.py's logic."""
    doc_entry = transforms_cache.get(doc_filename, {})
    page_transforms = doc_entry.get(str(page_number))
    if not page_transforms:
        return None
    for t in page_transforms:
        if "rotation" in t:
            return t["rotation"]
    return None


# ---------------------------------------------------------------------------
# Helpers — re-derive full-resolution bytes exactly as the original extraction
# ---------------------------------------------------------------------------


def _rasterize_pdf_page(pdf: Any, page_idx: int) -> bytes:
    page = pdf[page_idx]
    pixmap = page.get_pixmap(dpi=PDF_RENDER_DPI)
    image_bytes = pixmap.tobytes(_OUTPUT_FORMAT)
    del pixmap
    return image_bytes


def _convert_image_doc(raw_bytes: bytes) -> bytes:
    image = PILImage.open(io.BytesIO(raw_bytes))
    exif_data = image.info.get("exif")
    image = image.convert("RGB")
    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {"format": "JPEG"}
    if exif_data:
        save_kwargs["exif"] = exif_data
    image.save(buf, **save_kwargs)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# lines.json patching
# ---------------------------------------------------------------------------


def _patch_lines_json(
    target_storage: StorageBackend, submission_id: str, record: PageRecord
) -> None:
    """Add raw_image_* fields to an existing lines.json, if present."""
    if not record.lines_filename:
        return
    lines_path = f"{submission_id}/{record.lines_filename}"
    try:
        payload = json.loads(target_storage.read_text(lines_path))
    except Exception as exc:
        logger.warning("Backfill: cannot read lines JSON %s: %s", lines_path, exc)
        return

    payload["raw_image_filename"] = record.raw_image_filename
    payload["raw_image_width"] = record.raw_image_width
    payload["raw_image_height"] = record.raw_image_height

    try:
        target_storage.write_bytes(
            lines_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )
    except Exception as exc:
        logger.warning("Backfill: cannot write patched lines JSON %s: %s", lines_path, exc)


# ---------------------------------------------------------------------------
# Per-doc backfill
# ---------------------------------------------------------------------------


def _backfill_doc(
    doc_file: DocFile,
    pages: list[PageRecord],
    source_path: str,
    submission_id: str,
    source_storage: StorageBackend,
    target_storage: StorageBackend,
    transforms_cache: dict[str, Any],
) -> list[PageRecord]:
    """Backfill raw images for all *pages* belonging to one source doc.

    Returns a list the same length as *pages*: successfully backfilled pages
    get a new :class:`PageRecord` with raw fields populated; pages that could
    not be backfilled are returned unchanged.
    """
    doc_basename = Path(doc_file.stored_filename).stem
    doc_path = f"{source_path}/{doc_file.stored_filename}"

    try:
        raw_bytes = source_storage.read_bytes(doc_path)
    except Exception as exc:
        logger.warning(
            "Backfill: cannot read source doc %s for submission %s: %s",
            doc_path, submission_id, exc,
        )
        return list(pages)

    is_pdf = _is_pdf(doc_file)
    pdf = None
    if is_pdf:
        try:
            pdf = pymupdf.open(stream=raw_bytes, filetype="pdf")
        except Exception as exc:
            logger.warning(
                "Backfill: cannot open PDF %s for submission %s: %s",
                doc_path, submission_id, exc,
            )
            return list(pages)

    updated: list[PageRecord] = []
    try:
        for page_record in pages:
            page_number = page_record.page_number
            try:
                if is_pdf:
                    if page_number - 1 >= pdf.page_count:
                        logger.warning(
                            "Backfill: page %d out of range for %s (submission %s)",
                            page_number, doc_path, submission_id,
                        )
                        updated.append(page_record)
                        continue
                    image_bytes = _rasterize_pdf_page(pdf, page_number - 1)
                else:
                    image_bytes = _convert_image_doc(raw_bytes)

                rotation = (
                    _cached_rotation(transforms_cache, doc_file.stored_filename, page_number)
                    or 0
                )
                raw_image = build_raw_image(image_bytes, rotation)

                if raw_image is None:
                    logger.warning(
                        "Backfill: could not build raw image for %s page %d (submission %s)",
                        doc_file.stored_filename, page_number, submission_id,
                    )
                    updated.append(page_record)
                    continue

                raw_filename = _raw_page_filename(doc_basename, page_number)
                target_path = f"{submission_id}/{raw_filename}"
                target_storage.write_bytes(target_path, raw_image["bytes"])

                new_record = PageRecord(
                    submission_id=page_record.submission_id,
                    doc_filename=page_record.doc_filename,
                    page_number=page_record.page_number,
                    status=page_record.status,
                    error=page_record.error,
                    image_filename=page_record.image_filename,
                    lines_filename=page_record.lines_filename,
                    raw_image_filename=raw_filename,
                    raw_image_width=raw_image["width"],
                    raw_image_height=raw_image["height"],
                )
                _patch_lines_json(target_storage, submission_id, new_record)

                updated.append(new_record)
                logger.info(
                    "Backfilled raw image %s (%dx%d) for submission %s page %d",
                    raw_filename, raw_image["width"], raw_image["height"],
                    submission_id, page_number,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Backfill failed for submission %s, doc %s, page %d: %s",
                    submission_id, doc_file.stored_filename, page_number, exc,
                )
                updated.append(page_record)
    finally:
        if pdf is not None:
            pdf.close()

    return updated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_backfill(config: Config) -> None:
    """Backfill raw AVIF images for already-completed pages in target storage.

    Only touches submissions/pages that already exist in the target's
    ``pages.csv`` — no new submissions are discovered or processed from
    source beyond what's needed to re-read the specific documents already
    referenced there, and Surya line detection is never re-run.
    """
    # Local import to avoid a module-level circular import (coordinator
    # imports backfill lazily too, from inside run()).
    from transcriptor_worker.coordinator import (
        _build_storage,
        _check_source_readable,
        _check_target_writable,
    )

    logger.info("Starting raw-image backfill (BACKFILL_RAW_IMAGES=true)")

    source_storage, source_prefix = _build_storage(
        config.source_storage_type,
        config.source_storage_path,
        aws_access_key_id=config.source_aws_access_key_id,
        aws_secret_access_key=config.source_aws_secret_access_key,
        aws_region=config.source_aws_region,
    )
    target_storage, target_prefix = _build_storage(
        config.target_storage_type,
        config.target_storage_path,
        aws_access_key_id=config.target_aws_access_key_id,
        aws_secret_access_key=config.target_aws_secret_access_key,
        aws_region=config.target_aws_region,
    )

    _check_source_readable(source_storage, source_prefix)
    _check_target_writable(target_storage, target_prefix)

    pages_csv_path = f"{target_prefix}/pages.csv" if target_prefix else "pages.csv"
    all_pages = load_pages_csv(target_storage, pages_csv_path)

    if not all_pages:
        logger.info(
            "No pages.csv found (or empty) at %s — nothing to backfill.", pages_csv_path
        )
        return

    needs_backfill = [
        p
        for p in all_pages
        if p.status == "completed" and p.image_filename and not p.raw_image_filename
    ]

    if not needs_backfill:
        logger.info(
            "Backfill: all %d page record(s) already have raw images (or are not eligible).",
            len(all_pages),
        )
        return

    submission_ids = sorted({p.submission_id for p in needs_backfill})
    if config.max_submissions is not None and len(submission_ids) > config.max_submissions:
        submission_ids = submission_ids[: config.max_submissions]
        allowed = set(submission_ids)
        needs_backfill = [p for p in needs_backfill if p.submission_id in allowed]
        logger.info(
            "MAX_SUBMISSIONS=%d: limiting backfill to %d submission(s)",
            config.max_submissions,
            len(submission_ids),
        )

    logger.info(
        "Backfill: %d page(s) across %d submission(s) missing raw images",
        len(needs_backfill),
        len(submission_ids),
    )

    # Resolve submission_id -> source_path. This walks source for desc.json
    # locations only, to map IDs already known from target's pages.csv back
    # to their source folder — it does not process or write anything for
    # submissions that aren't already present in target's pages.csv.
    source_submissions = discover_submissions(source_storage, source_prefix)
    source_path_by_id = {s.id: s.source_path for s in source_submissions}

    pages_by_submission: dict[str, list[PageRecord]] = defaultdict(list)
    for p in needs_backfill:
        pages_by_submission[p.submission_id].append(p)

    updated_by_key: dict[tuple[str, str, int], PageRecord] = {}

    for submission_id, pages in pages_by_submission.items():
        source_path = source_path_by_id.get(submission_id)
        if source_path is None:
            logger.warning(
                "Backfill: submission %s not found in source — skipping %d page(s)",
                submission_id,
                len(pages),
            )
            continue

        try:
            desc_bytes = source_storage.read_text(f"{source_path}/desc.json")
            desc = DescJson.from_dict(json.loads(desc_bytes))
        except Exception as exc:
            logger.warning(
                "Backfill: cannot read desc.json for submission %s: %s", submission_id, exc
            )
            continue

        doc_by_filename = {d.stored_filename: d for d in desc.files}
        transforms_cache = _load_transforms(source_storage, source_path)

        pages_by_doc: dict[str, list[PageRecord]] = defaultdict(list)
        for p in pages:
            pages_by_doc[p.doc_filename].append(p)

        for doc_filename, doc_pages in pages_by_doc.items():
            doc_file = doc_by_filename.get(doc_filename)
            if doc_file is None:
                logger.warning(
                    "Backfill: doc %s not found in desc.json for submission %s",
                    doc_filename,
                    submission_id,
                )
                continue
            updated = _backfill_doc(
                doc_file,
                doc_pages,
                source_path,
                submission_id,
                source_storage,
                target_storage,
                transforms_cache,
            )
            for rec in updated:
                updated_by_key[(rec.submission_id, rec.doc_filename, rec.page_number)] = rec

    success_count = sum(1 for rec in updated_by_key.values() if rec.raw_image_filename)

    if not updated_by_key:
        logger.info("Backfill made no changes.")
        return

    merged_pages = [
        updated_by_key.get((p.submission_id, p.doc_filename, p.page_number), p)
        for p in all_pages
    ]

    save_pages_csv(merged_pages, target_storage, pages_csv_path)
    logger.info(
        "Backfill complete — wrote %d page record(s) to %s (%d newly backfilled)",
        len(merged_pages),
        pages_csv_path,
        success_count,
    )
