"""Worker — per-submission processing logic dispatched to sub-processes.

Each worker process:
1. Has its Surya model loaded once via :func:`init_worker` (Pool initializer).
2. Receives one :class:`~transcriptor_worker.models.Submission` at a time via
   :func:`process_submission`.

Because ``boto3`` clients are not picklable, storage backends are reconstructed
inside the sub-process from plain config values (dicts) rather than being
passed directly.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from typing import Any

from transcriptor_worker.extraction.lines import init_surya_model, process_page_lines
from transcriptor_worker.extraction.pages import extract_pages
from transcriptor_worker.models import PageRecord, Submission, SubmissionRecord
from transcriptor_worker.storage.base import StorageBackend
from transcriptor_worker.storage.local import LocalStorageBackend
from transcriptor_worker.storage.s3 import S3StorageBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level model handle — set once by init_worker()
# ---------------------------------------------------------------------------

_surya_model: Any = None


# ---------------------------------------------------------------------------
# Storage config (picklable)
# ---------------------------------------------------------------------------


@dataclass
class StorageConfig:
    """Picklable description of a storage backend.

    Passed to sub-processes instead of the backend itself (boto3 clients are
    not picklable).
    """

    storage_type: str          # "local" | "s3"
    storage_path: str          # local root or s3://bucket/prefix
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = None


def _build_backend(cfg: StorageConfig) -> tuple[StorageBackend, str]:
    """Reconstruct a storage backend from a :class:`StorageConfig`.

    Returns ``(backend, root_prefix)``.
    """
    if cfg.storage_type == "local":
        return LocalStorageBackend(cfg.storage_path), ""

    if cfg.storage_type == "s3":
        without_scheme = cfg.storage_path[len("s3://"):]
        bucket, _, prefix = without_scheme.partition("/")
        backend = S3StorageBackend(
            bucket,
            aws_access_key_id=cfg.aws_access_key_id,
            aws_secret_access_key=cfg.aws_secret_access_key,
            aws_region=cfg.aws_region,
        )
        return backend, prefix.rstrip("/")

    raise ValueError(f"Unknown storage type: {cfg.storage_type!r}")


# ---------------------------------------------------------------------------
# Pool initializer
# ---------------------------------------------------------------------------


def init_worker(
    text_threshold: float | None,
    blank_threshold: float | None,
) -> None:
    """Sub-process initializer: load the Surya model once and store it globally.

    Called by :class:`multiprocessing.Pool` before any work is dispatched.
    If model loading fails the worker process logs the error and continues
    with ``_surya_model = None``; every job dispatched to it will then
    produce a per-page ``failed`` record via the exception handler in
    :func:`process_submission`.

    Args:
        text_threshold: Passed to :func:`~transcriptor_worker.extraction.lines.init_surya_model`.
        blank_threshold: Passed to :func:`~transcriptor_worker.extraction.lines.init_surya_model`.
    """
    global _surya_model
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger.info("Worker process initialising — loading Surya model")
    try:
        _surya_model = init_surya_model(
            text_threshold=text_threshold,
            blank_threshold=blank_threshold,
        )
        logger.info("Worker process ready")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to load Surya model in worker process: %s — "
            "all jobs on this worker will fail at line extraction.",
            exc,
        )
        _surya_model = None


# ---------------------------------------------------------------------------
# Per-submission worker
# ---------------------------------------------------------------------------


def process_submission(
    submission: Submission,
    source_cfg: StorageConfig,
    target_cfg: StorageConfig,
    temp_dir: str,
) -> tuple[SubmissionRecord, list[PageRecord]]:
    """Process a single submission end-to-end.

    Steps:
    1. Reconstruct source and target storage backends.
    2. Extract page images from the submission's docs.
    3. Run Surya line detection on each successfully extracted page.
    4. Copy ``desc.json`` to target storage.
    5. Return a :class:`SubmissionRecord` (completed / failed) and all
       :class:`PageRecord` objects.

    Any unhandled exception causes the submission to be marked ``failed``
    without aborting the worker process.

    Args:
        submission: The submission to process.
        source_cfg: Picklable source storage configuration.
        target_cfg: Picklable target storage configuration.
        temp_dir: Root temp directory for this run.

    Returns:
        ``(SubmissionRecord, list[PageRecord])``
    """
    logger.info("Processing submission %s", submission.id)

    try:
        source_storage, _source_prefix = _build_backend(source_cfg)
        target_storage, _target_prefix = _build_backend(target_cfg)
    except Exception as exc:
        logger.error(
            "Failed to build storage for submission %s: %s", submission.id, exc
        )
        return (
            SubmissionRecord(
                submission_id=submission.id,
                status="failed",
                error=f"Storage init error: {exc}",
            ),
            [],
        )

    # ------------------------------------------------------------------
    # Stage 1: page extraction
    # ------------------------------------------------------------------
    try:
        page_records = extract_pages(
            submission, source_storage, target_storage, temp_dir
        )
    except Exception as exc:
        logger.error(
            "Page extraction failed for submission %s: %s",
            submission.id,
            traceback.format_exc(),
        )
        return (
            SubmissionRecord(
                submission_id=submission.id,
                status="failed",
                error=f"Page extraction error: {exc}",
            ),
            [],
        )

    # ------------------------------------------------------------------
    # Stage 2: line extraction on successfully extracted pages
    # ------------------------------------------------------------------
    updated_records: list[PageRecord] = []
    for pr in page_records:
        if pr.status != "pending" or not pr.image_filename:
            # Pass through failed / already-processed records unchanged.
            updated_records.append(pr)
            continue

        try:
            updated = process_page_lines(pr, _surya_model, target_storage, temp_dir)
        except Exception as exc:
            logger.warning(
                "Line extraction failed for %s/%s: %s",
                submission.id,
                pr.image_filename,
                exc,
            )
            updated = PageRecord(
                submission_id=pr.submission_id,
                doc_filename=pr.doc_filename,
                page_number=pr.page_number,
                status="failed",
                error=f"Line extraction error: {exc}",
                image_filename=pr.image_filename,
                lines_filename="",
            )
        updated_records.append(updated)

    # ------------------------------------------------------------------
    # Stage 3: copy desc.json to target
    # ------------------------------------------------------------------
    try:
        desc_src_path = f"{submission.source_path}/desc.json"
        desc_bytes = source_storage.read_bytes(desc_src_path)
        desc_target_path = f"{submission.id}/desc.json"
        target_storage.write_bytes(desc_target_path, desc_bytes)
        logger.debug(
            "Copied desc.json for submission %s → %s",
            submission.id,
            desc_target_path,
        )
    except Exception as exc:
        logger.warning(
            "Failed to copy desc.json for submission %s: %s", submission.id, exc
        )
        # Non-fatal: submission still considered (potentially) complete.

    # ------------------------------------------------------------------
    # Determine overall submission status
    # ------------------------------------------------------------------
    any_failed = any(r.status == "failed" for r in updated_records)
    all_failed = updated_records and all(r.status == "failed" for r in updated_records)

    if all_failed:
        status = "failed"
        error = "; ".join(
            r.error for r in updated_records if r.error
        )[:500]
    elif any_failed:
        status = "completed"   # partial success — some pages failed
        failed_pages = [r.image_filename or r.doc_filename for r in updated_records if r.status == "failed"]
        error = f"Some pages failed: {', '.join(failed_pages)}"
    else:
        status = "completed"
        error = ""

    logger.info(
        "Submission %s finished with status=%s (%d page(s), %d failed)",
        submission.id,
        status,
        len(updated_records),
        sum(1 for r in updated_records if r.status == "failed"),
    )

    return (
        SubmissionRecord(
            submission_id=submission.id,
            status=status,
            error=error,
        ),
        updated_records,
    )
