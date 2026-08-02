"""Tests for coordinator.py — work-queue slicing via MAX_SUBMISSIONS."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import multiprocessing

import pytest

from transcriptor_worker.coordinator import run
from transcriptor_worker.models import Submission


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_submission(idx: int) -> Submission:
    """Create a minimal Submission for testing."""
    return Submission(id=f"sub-{idx}", source_path=f"path/{idx}")


def _make_config(max_submissions: int | None = None) -> MagicMock:
    """Return a mock Config-like object."""
    cfg = MagicMock()
    cfg.source_storage_type = "local"
    cfg.source_storage_path = "/src"
    cfg.source_aws_access_key_id = None
    cfg.source_aws_secret_access_key = None
    cfg.source_aws_region = None
    cfg.target_storage_type = "local"
    cfg.target_storage_path = "/tgt"
    cfg.target_aws_access_key_id = None
    cfg.target_aws_secret_access_key = None
    cfg.target_aws_region = None
    cfg.worker_parallelism = 1
    cfg.max_submissions = max_submissions
    cfg.detector_text_threshold = None
    cfg.detector_blank_threshold = None
    cfg.submitter_fingerprint_salt = ""
    cfg.force_reprocess = False
    cfg.force_reprocess_metadata = False
    cfg.backfill_raw_images = False
    return cfg


# ---------------------------------------------------------------------------
# Fixtures / shared patches
# ---------------------------------------------------------------------------

_COORDINATOR = "transcriptor_worker.coordinator"


def _run_with_queue(queue: list[Submission], max_submissions: int | None) -> list[Submission]:
    """Run coordinator.run() with the given work queue and return the queue
    that was actually dispatched to the pool (captured via mock).

    All heavy collaborators (storage, manifest I/O, multiprocessing) are
    stubbed out so the test is fast and side-effect-free.
    """
    config = _make_config(max_submissions=max_submissions)

    dispatched: list[list[Submission]] = []

    # Minimal SubmissionRecord-like object returned by pool workers
    def fake_imap(fn, work):  # noqa: ANN001
        dispatched.append(list(work))
        return iter([])  # no results — that's fine; we just check the queue

    fake_pool = MagicMock()
    fake_pool.__enter__ = lambda s: fake_pool
    fake_pool.__exit__ = MagicMock(return_value=False)
    fake_pool.imap_unordered = fake_imap

    fake_ctx = MagicMock()
    fake_ctx.Pool.return_value = fake_pool

    with (
        patch(f"{_COORDINATOR}.Config.from_env", return_value=config),
        patch(f"{_COORDINATOR}._build_storage") as mock_build_storage,
        patch(f"{_COORDINATOR}._check_source_readable"),
        patch(f"{_COORDINATOR}._check_target_writable"),
        patch(f"{_COORDINATOR}.build_work_queue", return_value=queue),
        patch(f"{_COORDINATOR}.load_pages_csv", return_value=[]),
        patch(f"{_COORDINATOR}.load_submissions_csv", return_value={}),
        patch(f"{_COORDINATOR}.save_submissions_csv"),
        patch(f"{_COORDINATOR}.save_pages_csv"),
        patch(f"{_COORDINATOR}.multiprocessing.get_context", return_value=fake_ctx),
    ):
        # _build_storage returns (mock_backend, "")
        mock_build_storage.return_value = (MagicMock(), "")
        run()

    return dispatched[0] if dispatched else []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBackfillDispatch:
    def test_backfill_flag_short_circuits_normal_pipeline(self):
        """When BACKFILL_RAW_IMAGES is set, run() must delegate to
        run_backfill() and must not touch the normal work-queue pipeline."""
        config = _make_config()
        config.backfill_raw_images = True

        with (
            patch(f"{_COORDINATOR}.Config.from_env", return_value=config),
            patch(f"{_COORDINATOR}.build_work_queue") as mock_build_work_queue,
            patch("transcriptor_worker.backfill.run_backfill") as mock_run_backfill,
        ):
            run()

        mock_run_backfill.assert_called_once_with(config)
        mock_build_work_queue.assert_not_called()


class TestMaxSubmissionsSlicing:
    def test_no_limit_full_queue_dispatched(self):
        """When max_submissions is None the entire work queue is dispatched."""
        queue = [_make_submission(i) for i in range(5)]
        dispatched = _run_with_queue(queue, max_submissions=None)
        assert len(dispatched) == 5
        assert dispatched == queue

    def test_limit_truncates_queue(self):
        """When max_submissions=2, only the first 2 items are dispatched."""
        queue = [_make_submission(i) for i in range(5)]
        dispatched = _run_with_queue(queue, max_submissions=2)
        assert len(dispatched) == 2
        assert dispatched == queue[:2]

    def test_limit_exceeds_queue_length(self):
        """When max_submissions exceeds queue length, full queue is used (no error)."""
        queue = [_make_submission(i) for i in range(3)]
        dispatched = _run_with_queue(queue, max_submissions=10)
        assert len(dispatched) == 3
        assert dispatched == queue


class TestWorkerWatchdog:
    def test_hung_worker_aborts_run_with_inflight_log_message(self):
        """A result that never arrives must abort the run (RuntimeError) and name
        the in-flight submissions — instead of hanging forever."""
        queue = [_make_submission(i) for i in range(3)]
        config = _make_config()

        # Simulate a worker that hangs: the pool result iterator times out forever.
        class HungResultIterator:
            def next(self, timeout=None):  # noqa: ANN001
                raise multiprocessing.TimeoutError("timeout")

        fake_pool = MagicMock()
        fake_pool.__enter__ = lambda s: fake_pool
        fake_pool.__exit__ = MagicMock(return_value=False)
        fake_pool.imap_unordered = MagicMock(return_value=HungResultIterator())

        fake_ctx = MagicMock()
        fake_ctx.Pool.return_value = fake_pool

        # Force the watchdog to trip almost immediately.
        config.worker_result_stall_log = 1
        config.worker_result_timeout = 1

        with (
            patch(f"{_COORDINATOR}.Config.from_env", return_value=config),
            patch(f"{_COORDINATOR}._build_storage") as mock_build_storage,
            patch(f"{_COORDINATOR}._check_source_readable"),
            patch(f"{_COORDINATOR}._check_target_writable"),
            patch(f"{_COORDINATOR}.build_work_queue", return_value=queue),
            patch(f"{_COORDINATOR}.load_pages_csv", return_value=[]),
            patch(f"{_COORDINATOR}.load_submissions_csv", return_value={}),
            patch(f"{_COORDINATOR}.multiprocessing.get_context", return_value=fake_ctx),
        ):
            mock_build_storage.return_value = (MagicMock(), "")
            with pytest.raises(RuntimeError, match="no worker result"):
                run()
