"""Tests for manifest.py — CSV round-trip for submissions and pages."""

from __future__ import annotations

import pytest

from transcriptor_worker.manifest import (
    load_pages_csv,
    load_submissions_csv,
    save_pages_csv,
    save_submissions_csv,
)
from transcriptor_worker.models import PageRecord, SubmissionRecord
from transcriptor_worker.storage.local import LocalStorageBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(str(tmp_path))


# ---------------------------------------------------------------------------
# SubmissionRecord round-trip
# ---------------------------------------------------------------------------


class TestSubmissionsCSV:
    def test_empty_list_writes_only_header(self, storage):
        save_submissions_csv([], storage, "submissions.csv")
        loaded = load_submissions_csv(storage, "submissions.csv")
        assert loaded == {}

    def test_single_completed(self, storage):
        records = [SubmissionRecord(submission_id="abc123", status="completed", error="")]
        save_submissions_csv(records, storage, "submissions.csv")
        loaded = load_submissions_csv(storage, "submissions.csv")
        assert "abc123" in loaded
        assert loaded["abc123"].status == "completed"
        assert loaded["abc123"].error == ""

    def test_single_failed(self, storage):
        records = [SubmissionRecord(submission_id="dead01", status="failed", error="boom")]
        save_submissions_csv(records, storage, "submissions.csv")
        loaded = load_submissions_csv(storage, "submissions.csv")
        assert loaded["dead01"].status == "failed"
        assert loaded["dead01"].error == "boom"

    def test_multiple_records(self, storage):
        records = [
            SubmissionRecord("s1", "completed"),
            SubmissionRecord("s2", "failed", "err"),
            SubmissionRecord("s3", "completed"),
        ]
        save_submissions_csv(records, storage, "submissions.csv")
        loaded = load_submissions_csv(storage, "submissions.csv")
        assert len(loaded) == 3
        assert loaded["s1"].status == "completed"
        assert loaded["s2"].error == "err"

    def test_missing_file_returns_empty_dict(self, storage):
        result = load_submissions_csv(storage, "missing.csv")
        assert result == {}

    def test_roundtrip_preserves_all_fields(self, storage):
        original = [
            SubmissionRecord("id-1", "completed", ""),
            SubmissionRecord("id-2", "failed", "something went wrong"),
        ]
        save_submissions_csv(original, storage, "sub.csv")
        loaded = load_submissions_csv(storage, "sub.csv")
        for rec in original:
            loaded_rec = loaded[rec.submission_id]
            assert loaded_rec.submission_id == rec.submission_id
            assert loaded_rec.status == rec.status
            assert loaded_rec.error == rec.error

    def test_error_field_with_comma(self, storage):
        """Error messages containing commas must survive CSV encoding."""
        records = [
            SubmissionRecord("id-x", "failed", "step1, step2, step3 failed")
        ]
        save_submissions_csv(records, storage, "sub.csv")
        loaded = load_submissions_csv(storage, "sub.csv")
        assert loaded["id-x"].error == "step1, step2, step3 failed"


# ---------------------------------------------------------------------------
# PageRecord round-trip
# ---------------------------------------------------------------------------


class TestPagesCSV:
    def test_empty_list_writes_only_header(self, storage):
        save_pages_csv([], storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert loaded == []

    def test_single_completed_page(self, storage):
        records = [
            PageRecord(
                submission_id="s1",
                doc_filename="doc.pdf",
                page_number=1,
                status="completed",
                image_filename="doc_p1.jpg",
                lines_filename="doc_p1.jpg.json",
            )
        ]
        save_pages_csv(records, storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert len(loaded) == 1
        r = loaded[0]
        assert r.submission_id == "s1"
        assert r.doc_filename == "doc.pdf"
        assert r.page_number == 1
        assert r.status == "completed"
        assert r.image_filename == "doc_p1.jpg"
        assert r.lines_filename == "doc_p1.jpg.json"

    def test_page_number_preserved_as_int(self, storage):
        records = [PageRecord("s1", "f.pdf", 42, "completed")]
        save_pages_csv(records, storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert loaded[0].page_number == 42
        assert isinstance(loaded[0].page_number, int)

    def test_failed_page_empty_filenames(self, storage):
        records = [
            PageRecord("s1", "f.pdf", 1, "failed", error="oops", image_filename="", lines_filename="")
        ]
        save_pages_csv(records, storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert loaded[0].status == "failed"
        assert loaded[0].error == "oops"
        assert loaded[0].image_filename == ""
        assert loaded[0].lines_filename == ""

    def test_multiple_pages_order_preserved(self, storage):
        records = [PageRecord("s1", "doc.pdf", i, "completed") for i in range(1, 6)]
        save_pages_csv(records, storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert [r.page_number for r in loaded] == [1, 2, 3, 4, 5]

    def test_missing_file_returns_empty_list(self, storage):
        result = load_pages_csv(storage, "no_such_pages.csv")
        assert result == []

    def test_roundtrip_all_fields(self, storage):
        original = [
            PageRecord(
                submission_id="sub-abc",
                doc_filename="scan.pdf",
                page_number=3,
                status="completed",
                error="",
                image_filename="scan_p3.jpg",
                lines_filename="scan_p3.jpg.json",
            ),
            PageRecord(
                submission_id="sub-abc",
                doc_filename="scan.pdf",
                page_number=4,
                status="failed",
                error="line extraction timeout",
                image_filename="scan_p4.jpg",
                lines_filename="",
            ),
        ]
        save_pages_csv(original, storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert len(loaded) == len(original)
        for orig, loaded_rec in zip(original, loaded):
            assert orig.submission_id == loaded_rec.submission_id
            assert orig.doc_filename == loaded_rec.doc_filename
            assert orig.page_number == loaded_rec.page_number
            assert orig.status == loaded_rec.status
            assert orig.error == loaded_rec.error
            assert orig.image_filename == loaded_rec.image_filename
            assert orig.lines_filename == loaded_rec.lines_filename

    def test_error_with_comma_survives(self, storage):
        records = [PageRecord("s1", "f.pdf", 1, "failed", error="a, b, c")]
        save_pages_csv(records, storage, "pages.csv")
        loaded = load_pages_csv(storage, "pages.csv")
        assert loaded[0].error == "a, b, c"
