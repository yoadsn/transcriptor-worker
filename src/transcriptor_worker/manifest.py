"""CSV manifest load/save helpers for submissions and pages."""

from __future__ import annotations

import csv
import io
import logging

from transcriptor_worker.models import PageRecord, SubmissionRecord
from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_SUBMISSION_FIELDS = ["submission_id", "status", "error"]
_PAGE_FIELDS = [
    "submission_id",
    "doc_filename",
    "page_number",
    "status",
    "error",
    "image_filename",
    "lines_filename",
]


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_submissions_csv(
    storage: StorageBackend, path: str
) -> dict[str, SubmissionRecord]:
    """Parse ``submissions.csv`` from *storage* at *path*.

    Returns a dict keyed by ``submission_id``.  Returns an empty dict if the
    file does not exist.
    """
    if not storage.exists(path):
        logger.info("submissions.csv not found at %s — starting fresh", path)
        return {}

    text = storage.read_text(path)
    reader = csv.DictReader(io.StringIO(text))
    records: dict[str, SubmissionRecord] = {}
    for row in reader:
        rec = SubmissionRecord.from_dict(row)
        records[rec.submission_id] = rec
    logger.info("Loaded %d submission records from %s", len(records), path)
    return records


def load_pages_csv(storage: StorageBackend, path: str) -> list[PageRecord]:
    """Parse ``pages.csv`` from *storage* at *path*.

    Returns a list of :class:`PageRecord`.  Returns an empty list if the file
    does not exist.
    """
    if not storage.exists(path):
        logger.info("pages.csv not found at %s — starting fresh", path)
        return []

    text = storage.read_text(path)
    reader = csv.DictReader(io.StringIO(text))
    records: list[PageRecord] = []
    for row in reader:
        records.append(PageRecord.from_dict(row))
    logger.info("Loaded %d page records from %s", len(records), path)
    return records


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_submissions_csv(
    records: list[SubmissionRecord],
    storage: StorageBackend,
    path: str,
) -> None:
    """Write *records* as ``submissions.csv`` to *storage* at *path*."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_SUBMISSION_FIELDS, lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec.to_dict())
    storage.write_text(path, buf.getvalue())
    logger.info("Saved %d submission records to %s", len(records), path)


def save_pages_csv(
    records: list[PageRecord],
    storage: StorageBackend,
    path: str,
) -> None:
    """Write *records* as ``pages.csv`` to *storage* at *path*."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_PAGE_FIELDS, lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec.to_dict())
    storage.write_text(path, buf.getvalue())
    logger.info("Saved %d page records to %s", len(records), path)
