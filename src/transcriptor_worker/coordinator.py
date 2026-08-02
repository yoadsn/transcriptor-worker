"""Coordinator — orchestrates source discovery, manifest loading, and work dispatch."""

from __future__ import annotations

import functools
import logging
import multiprocessing
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from transcriptor_worker.config import Config
from transcriptor_worker.discovery import discover_submissions
from transcriptor_worker.manifest import (
    load_pages_csv,
    load_submissions_csv,
    save_pages_csv,
    save_submissions_csv,
)
from transcriptor_worker.models import PageRecord, Submission, SubmissionRecord
from transcriptor_worker.storage.base import StorageBackend
from transcriptor_worker.storage.local import LocalStorageBackend
from transcriptor_worker.storage.s3 import S3StorageBackend
from transcriptor_worker.worker import StorageConfig, init_worker, process_submission

logger = logging.getLogger(__name__)


def _coerce_opt_int(value: object, default: int) -> int | None:
    """Return *value* if it is a usable int, *default* if it is a mock/other,
    or ``None`` if *value* is explicitly ``None`` (feature disabled)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return default


def _in_flight_ids(
    work_queue: list[Submission], collected_ids: set[str]
) -> list[str]:
    """Return submission ids that were dispatched but never returned.

    These are the candidate tasks a stuck worker might be chewing on.
    """
    return [s.id for s in work_queue if s.id not in collected_ids]


def _next_result(result_iter: Any, timeout: float) -> tuple[SubmissionRecord, list[PageRecord]]:
    """Fetch the next result, honouring *timeout*.

    ``multiprocessing.pool`` result iterators expose a ``.next(timeout)``
    method.  Fall back to a plain ``next(...)`` for mocks/fakes in tests.
    """
    nxt = getattr(result_iter, "next", None)
    if callable(nxt):
        return nxt(timeout)
    return next(result_iter)


# ---------------------------------------------------------------------------
# Storage factory
# ---------------------------------------------------------------------------


def _build_storage(
    storage_type: str,
    storage_path: str,
    *,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    aws_region: str | None = None,
) -> tuple[StorageBackend, str]:
    """Construct a storage backend from config values.

    Returns:
        Tuple of (backend, root_prefix) where *root_prefix* is the path /
        key prefix within the backend to use as the root.  For local storage
        this is always ``""`` (the backend's root is the filesystem path).
        For S3, the prefix is the key prefix extracted from the URI.
    """
    if storage_type == "local":
        return LocalStorageBackend(storage_path), ""

    if storage_type == "s3":
        # Parse s3://bucket/prefix
        if not storage_path.startswith("s3://"):
            raise ValueError(
                f"S3 storage path must start with 's3://', got: {storage_path!r}"
            )
        without_scheme = storage_path[len("s3://"):]
        bucket, _, prefix = without_scheme.partition("/")
        backend = S3StorageBackend(
            bucket,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_region=aws_region,
        )
        return backend, prefix.rstrip("/")

    raise ValueError(f"Unknown storage type: {storage_type!r}")


# ---------------------------------------------------------------------------
# Startup checks
# ---------------------------------------------------------------------------


def _check_source_readable(storage: StorageBackend, prefix: str) -> None:
    """Verify that source storage is readable.

    Performs a lightweight ``list_prefixes`` call.  Raises :class:`RuntimeError`
    with a descriptive message if the check fails.

    Args:
        storage: Source storage backend.
        prefix: Root prefix to list under (empty string for the bucket root).

    Raises:
        RuntimeError: If the storage cannot be read.
    """
    try:
        storage.list_prefixes(prefix)
        logger.info("Source storage read check passed.")
    except Exception as exc:
        raise RuntimeError(
            f"Source storage is not readable: {exc}\n"
            "Check SOURCE_STORAGE_TYPE, SOURCE_STORAGE_PATH, and source AWS credentials."
        ) from exc


def _check_target_writable(storage: StorageBackend, prefix: str) -> None:
    """Verify that target storage is writable.

    Writes a small probe object and immediately deletes it.  Raises
    :class:`RuntimeError` with a descriptive message if the check fails.

    Args:
        storage: Target storage backend.
        prefix: Root prefix for the target (empty string for the bucket root).

    Raises:
        RuntimeError: If the storage cannot be written to.
    """
    probe_name = f"_probe_{uuid.uuid4().hex}"
    probe_path = f"{prefix}/{probe_name}" if prefix else probe_name
    probe_data = b"probe"
    try:
        storage.write_bytes(probe_path, probe_data)
        logger.debug("Target write probe written: %s", probe_path)
    except Exception as exc:
        raise RuntimeError(
            f"Target storage is not writable: {exc}\n"
            "Check TARGET_STORAGE_TYPE, TARGET_STORAGE_PATH, and target AWS credentials."
        ) from exc

    try:
        storage.delete(probe_path)
        logger.debug("Target write probe deleted: %s", probe_path)
    except Exception as exc:
        # Non-fatal — we already confirmed write access; log a warning.
        logger.warning(
            "Could not delete write probe %s: %s (this is not critical)", probe_path, exc
        )

    logger.info("Target storage write check passed.")


# ---------------------------------------------------------------------------
# Work queue building
# ---------------------------------------------------------------------------


def build_work_queue(
    config: Config,
    source_storage: StorageBackend,
    target_storage: StorageBackend,
    source_prefix: str,
    target_prefix: str,
    force_reprocess: bool = False,
) -> list[Submission]:
    """Discover submissions and filter out already-completed ones.

    Args:
        config: Loaded pipeline configuration.
        source_storage: Source storage backend.
        target_storage: Target storage backend.
        source_prefix: Root prefix within source storage.
        target_prefix: Root prefix within target storage.
        force_reprocess: If True, include already-completed submissions.

    Returns:
        Ordered list of :class:`Submission` objects to process.
    """
    # Load existing target manifests
    submissions_csv_path = (
        f"{target_prefix}/submissions.csv" if target_prefix else "submissions.csv"
    )
    existing = load_submissions_csv(target_storage, submissions_csv_path)
    logger.info(
        "Existing manifest: %d submissions (%d completed)",
        len(existing),
        sum(1 for r in existing.values() if r.status == "completed"),
    )

    # Discover all submissions in source
    all_submissions = discover_submissions(source_storage, source_prefix)

    # Filter: skip completed (unless force_reprocess); include new and failed
    work_queue: list[Submission] = []
    skipped = 0
    for sub in all_submissions:
        record = existing.get(sub.id)
        if record and record.status == "completed" and not force_reprocess:
            skipped += 1
            logger.debug("Skipping completed submission %s", sub.id)
        else:
            work_queue.append(sub)

    logger.info(
        "Work queue: %d submissions to process, %d skipped (completed)%s",
        len(work_queue),
        skipped,
        " (force_reprocess=True)" if force_reprocess else "",
    )
    return work_queue


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Main entry point for the coordinator.

    1. Load config.
    2. Discover submissions and build a filtered work queue.
    3. Dispatch work to a ``multiprocessing.Pool`` (spawn context).
    4. Collect results and write final manifests to target storage.
    """
    config = Config.from_env()

    logger.info("Starting transcriptor-worker coordinator")

    if config.backfill_raw_images:
        from transcriptor_worker.backfill import run_backfill

        run_backfill(config)
        return

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

    # ------------------------------------------------------------------
    # Startup checks — validate credentials before doing any real work.
    # ------------------------------------------------------------------
    logger.info("Running startup storage checks…")
    try:
        _check_source_readable(source_storage, source_prefix)
        _check_target_writable(target_storage, target_prefix)
    except RuntimeError as exc:
        logger.error("Startup check failed: %s", exc)
        sys.exit(1)
    logger.info("Startup storage checks passed.")

    work_queue = build_work_queue(
        config,
        source_storage,
        target_storage,
        source_prefix,
        target_prefix,
        force_reprocess=config.force_reprocess or config.force_reprocess_metadata,
    )

    if config.max_submissions is not None:
        original_len = len(work_queue)
        work_queue = work_queue[: config.max_submissions]
        logger.info(
            "MAX_SUBMISSIONS=%d: limiting work queue from %d to %d submission(s)",
            config.max_submissions,
            original_len,
            len(work_queue),
        )

    if not work_queue:
        logger.info("Nothing to do — all submissions already completed.")
        return

    # ------------------------------------------------------------------
    # Carry forward already-completed page records from target manifest
    # so the final CSV is a full picture, not just this run's output.
    # Skipped in metadata-only mode (manifests are not touched).
    # ------------------------------------------------------------------
    carried_sub_records: list[SubmissionRecord] = []
    carried_page_records: list[PageRecord] = []

    if not config.force_reprocess_metadata:
        pages_csv_path = (
            f"{target_prefix}/pages.csv" if target_prefix else "pages.csv"
        )
        existing_pages = load_pages_csv(target_storage, pages_csv_path)
        submissions_csv_path = (
            f"{target_prefix}/submissions.csv" if target_prefix else "submissions.csv"
        )
        existing_submissions = load_submissions_csv(target_storage, submissions_csv_path)

        # Carry-forward rows for submissions we are NOT re-processing.
        work_ids = {s.id for s in work_queue}
        carried_sub_records = [
            r for r in existing_submissions.values() if r.submission_id not in work_ids
        ]
        carried_page_records = [
            r for r in existing_pages if r.submission_id not in work_ids
        ]

    # ------------------------------------------------------------------
    # Build picklable storage configs for worker sub-processes.
    # ------------------------------------------------------------------
    source_cfg = StorageConfig(
        storage_type=config.source_storage_type,
        storage_path=config.source_storage_path,
        aws_access_key_id=config.source_aws_access_key_id,
        aws_secret_access_key=config.source_aws_secret_access_key,
        aws_region=config.source_aws_region,
    )
    target_cfg = StorageConfig(
        storage_type=config.target_storage_type,
        storage_path=config.target_storage_path,
        aws_access_key_id=config.target_aws_access_key_id,
        aws_secret_access_key=config.target_aws_secret_access_key,
        aws_region=config.target_aws_region,
    )

    # ------------------------------------------------------------------
    # Use a temp directory for this run's intermediate files.
    # For a local target the temp dir IS the target root (no copy needed).
    # ------------------------------------------------------------------
    if config.target_storage_type == "local":
        run_temp_dir = config.target_storage_path
        _own_temp = None
    else:
        _own_temp = tempfile.mkdtemp(prefix="transcriptor_worker_")
        run_temp_dir = _own_temp

    logger.info(
        "Dispatching %d submission(s) to %d worker(s) (spawn)",
        len(work_queue),
        config.worker_parallelism,
    )

    # ------------------------------------------------------------------
    # Spawn pool — explicitly use "spawn" to avoid fork+PyTorch deadlocks.
    # ------------------------------------------------------------------
    ctx = multiprocessing.get_context("spawn")

    worker_fn = functools.partial(
        process_submission,
        source_cfg=source_cfg,
        target_cfg=target_cfg,
        temp_dir=run_temp_dir,
    )

    new_sub_records: list[SubmissionRecord] = []
    new_page_records: list[PageRecord] = []

    with ctx.Pool(
        processes=config.worker_parallelism,
        initializer=init_worker,
        initargs=(
            config.detector_text_threshold,
            config.detector_blank_threshold,
            config.submitter_fingerprint_salt,
            config.force_reprocess_metadata,
        ),
    ) as pool:
        result_iter = pool.imap_unordered(worker_fn, work_queue)
        collected_ids: set[str] = set()
        pending = len(work_queue)

        # Worker-result watchdog.  A hung worker (e.g. a pathological PDF that
        # never returns) would otherwise make `imap_unordered` block forever,
        # silently stalling the whole run.  Instead we bound the wait: log the
        # in-flight submissions after a stall, and abort after a hard timeout.
        stall_log_s = _coerce_opt_int(config.worker_result_stall_log, 90)
        hard_timeout_s = _coerce_opt_int(config.worker_result_timeout, 900)
        poll_s = 10
        logger.info(
            "Worker-result watchdog: WARN if no result for %ss, ABORT after %ss",
            stall_log_s if stall_log_s is not None else "off",
            hard_timeout_s if hard_timeout_s is not None else "off",
        )

        last_result = time.monotonic()
        stall_warned = False
        while pending > 0:
            try:
                sub_record, page_records = _next_result(result_iter, poll_s)
            except StopIteration:
                logger.warning(
                    "Pool produced fewer results than expected (%d/%d collected) — "
                    "aborting collection.",
                    len(new_sub_records),
                    len(work_queue),
                )
                break
            except multiprocessing.TimeoutError:
                elapsed = time.monotonic() - last_result
                if (
                    stall_log_s is not None
                    and elapsed >= stall_log_s
                    and not stall_warned
                ):
                    in_flight = _in_flight_ids(work_queue, collected_ids)
                    logger.warning(
                        "No worker result for %.0fs — worker(s) appear stuck. "
                        "In-flight (dispatched, not yet collected) submission(s): %s",
                        elapsed,
                        in_flight,
                    )
                    stall_warned = True
                if hard_timeout_s is not None and elapsed >= hard_timeout_s:
                    in_flight = _in_flight_ids(work_queue, collected_ids)
                    raise RuntimeError(
                        "Coordinator watchdog: no worker result for "
                        f"{elapsed:.0f}s (limit {hard_timeout_s}s). "
                        f"In-flight submission(s): {in_flight}"
                    )
                continue

            last_result = time.monotonic()
            stall_warned = False
            pending -= 1
            collected_ids.add(sub_record.submission_id)
            new_sub_records.append(sub_record)
            new_page_records.extend(page_records)
            logger.info(
                "Collected result for submission %s (status=%s, pages=%d)",
                sub_record.submission_id,
                sub_record.status,
                len(page_records),
            )

    # ------------------------------------------------------------------
    # Write final manifests to target storage.
    # Skipped in metadata-only mode (manifests are not touched).
    # ------------------------------------------------------------------
    if not config.force_reprocess_metadata:
        all_sub_records = carried_sub_records + new_sub_records
        all_page_records = carried_page_records + new_page_records

        save_submissions_csv(all_sub_records, target_storage, submissions_csv_path)
        save_pages_csv(all_page_records, target_storage, pages_csv_path)

    completed = sum(1 for r in new_sub_records if r.status == "completed")
    failed = sum(1 for r in new_sub_records if r.status == "failed")
    logger.info(
        "Run complete — %d completed, %d failed out of %d processed",
        completed,
        failed,
        len(new_sub_records),
    )

    if _own_temp:
        import shutil
        try:
            shutil.rmtree(_own_temp)
        except OSError as exc:
            logger.warning("Could not clean up temp dir %s: %s", _own_temp, exc)
