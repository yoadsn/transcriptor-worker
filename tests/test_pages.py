"""Tests for extraction/pages.py — PDF rasterization and image pass-through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from transcriptor_worker.extraction.pages import extract_pages
from transcriptor_worker.models import Submission
from transcriptor_worker.storage.local import LocalStorageBackend

# Fixture files live next to this test module
FIXTURES = Path(__file__).parent / "fixtures"
TWO_PAGE_PDF = FIXTURES / "two_page.pdf"
SAMPLE_JPG = FIXTURES / "sample.jpg"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_desc_json(files: list[dict]) -> str:
    """Build a minimal desc.json string."""
    return json.dumps({"version": "1", "user": "test", "files": files})


@pytest.fixture()
def src(tmp_path):
    """Source storage rooted at a fresh temp dir."""
    return LocalStorageBackend(str(tmp_path / "src"))


@pytest.fixture()
def tgt(tmp_path):
    """Target storage rooted at a fresh temp dir."""
    return LocalStorageBackend(str(tmp_path / "tgt"))


@pytest.fixture()
def temp_dir(tmp_path):
    """A temp dir string for page images."""
    d = tmp_path / "tmp"
    d.mkdir()
    return str(d)


def _write_submission(
    tmp_path: Path, sub_id: str, files: dict[str, bytes], desc_files: list[dict]
) -> None:
    """Populate the source with a submission directory."""
    sub_dir = tmp_path / "src" / sub_id
    sub_dir.mkdir(parents=True)
    for filename, data in files.items():
        (sub_dir / filename).write_bytes(data)
    (sub_dir / "desc.json").write_text(_make_desc_json(desc_files))


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------


class TestPDFExtraction:
    def test_two_page_pdf_yields_two_records(self, tmp_path, src, tgt, temp_dir):
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": "application/pdf",
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        assert len(records) == 2
        assert all(r.status == "pending" for r in records)
        assert all(r.submission_id == "sub1" for r in records)

    def test_pdf_page_numbers_are_one_based(self, tmp_path, src, tgt, temp_dir):
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        page_numbers = sorted(r.page_number for r in records)
        assert page_numbers == [1, 2]

    def test_pdf_image_filenames(self, tmp_path, src, tgt, temp_dir):
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        filenames = sorted(r.image_filename for r in records)
        assert filenames == ["doc_p1.jpg", "doc_p2.jpg"]

    def test_pdf_page_images_written_to_target(self, tmp_path, src, tgt, temp_dir):
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        extract_pages(submission, src, tgt, temp_dir)

        assert tgt.exists("sub1/doc_p1.jpg")
        assert tgt.exists("sub1/doc_p2.jpg")

    def test_pdf_page_images_written_to_temp(self, tmp_path, src, tgt, temp_dir):
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        extract_pages(submission, src, tgt, temp_dir)

        assert (Path(temp_dir) / "sub1" / "doc_p1.jpg").exists()
        assert (Path(temp_dir) / "sub1" / "doc_p2.jpg").exists()

    def test_pdf_output_is_valid_jpeg(self, tmp_path, src, tgt, temp_dir):
        """Each written page image should start with the JPEG magic bytes."""
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        extract_pages(submission, src, tgt, temp_dir)

        jpeg_magic = b"\xff\xd8"
        data = tgt.read_bytes("sub1/doc_p1.jpg")
        assert data[:2] == jpeg_magic

    def test_null_mime_type_pdf_detected_by_extension(
        self, tmp_path, src, tgt, temp_dir
    ):
        """PDF detection falls back to .pdf extension when mime_type is null."""
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [
                {
                    "stored_filename": "doc.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub1", source_path="sub1")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records
        # If we get 2 records, PDF was detected correctly
        assert len(records) == 2


# ---------------------------------------------------------------------------
# Image pass-through
# ---------------------------------------------------------------------------


class TestImagePassthrough:
    def test_single_jpeg_yields_one_record(self, tmp_path, src, tgt, temp_dir):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        _write_submission(
            tmp_path,
            "sub2",
            {"photo.jpg": jpg_bytes},
            [
                {
                    "stored_filename": "photo.jpg",
                    "file_extension": ".jpg",
                    "mime_type": "image/jpeg",
                }
            ],
        )
        submission = Submission(id="sub2", source_path="sub2")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        assert len(records) == 1
        assert records[0].status == "pending"
        assert records[0].page_number == 1

    def test_image_filename_convention(self, tmp_path, src, tgt, temp_dir):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        _write_submission(
            tmp_path,
            "sub2",
            {"photo.jpg": jpg_bytes},
            [
                {
                    "stored_filename": "photo.jpg",
                    "file_extension": ".jpg",
                    "mime_type": "image/jpeg",
                }
            ],
        )
        submission = Submission(id="sub2", source_path="sub2")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        assert records[0].image_filename == "photo_p1.jpg"

    def test_image_written_to_target(self, tmp_path, src, tgt, temp_dir):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        _write_submission(
            tmp_path,
            "sub2",
            {"photo.jpg": jpg_bytes},
            [
                {
                    "stored_filename": "photo.jpg",
                    "file_extension": ".jpg",
                    "mime_type": "image/jpeg",
                }
            ],
        )
        submission = Submission(id="sub2", source_path="sub2")
        extract_pages(submission, src, tgt, temp_dir)

        assert tgt.exists("sub2/photo_p1.jpg")

    def test_image_output_is_valid_jpeg(self, tmp_path, src, tgt, temp_dir):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        _write_submission(
            tmp_path,
            "sub2",
            {"photo.jpg": jpg_bytes},
            [
                {
                    "stored_filename": "photo.jpg",
                    "file_extension": ".jpg",
                    "mime_type": "image/jpeg",
                }
            ],
        )
        submission = Submission(id="sub2", source_path="sub2")
        extract_pages(submission, src, tgt, temp_dir)

        data = tgt.read_bytes("sub2/photo_p1.jpg")
        assert data[:2] == b"\xff\xd8"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_missing_desc_json_returns_failed_record(
        self, tmp_path, src, tgt, temp_dir
    ):
        # Write a submission folder but no desc.json
        (tmp_path / "src" / "sub-bad").mkdir(parents=True)
        submission = Submission(id="sub-bad", source_path="sub-bad")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        assert len(records) == 1
        assert records[0].status == "failed"
        assert (
            "desc.json" in records[0].error.lower() or "Cannot read" in records[0].error
        )

    def test_corrupted_pdf_yields_failed_record(self, tmp_path, src, tgt, temp_dir):
        _write_submission(
            tmp_path,
            "sub-corrupt",
            {"bad.pdf": b"not a pdf at all"},
            [
                {
                    "stored_filename": "bad.pdf",
                    "file_extension": ".pdf",
                    "mime_type": None,
                }
            ],
        )
        submission = Submission(id="sub-corrupt", source_path="sub-corrupt")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        assert len(records) >= 1
        # At least one record should be failed
        assert any(r.status == "failed" for r in records)

    def test_missing_doc_file_yields_failed_record(self, tmp_path, src, tgt, temp_dir):
        """desc.json references a file that doesn't exist."""
        sub_dir = tmp_path / "src" / "sub-missing"
        sub_dir.mkdir(parents=True)
        (sub_dir / "desc.json").write_text(
            _make_desc_json(
                [
                    {
                        "stored_filename": "ghost.pdf",
                        "file_extension": ".pdf",
                        "mime_type": None,
                    }
                ]
            )
        )
        # ghost.pdf is NOT written

        submission = Submission(id="sub-missing", source_path="sub-missing")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        assert len(records) >= 1
        assert any(r.status == "failed" for r in records)

    def test_failed_doc_does_not_abort_others(self, tmp_path, src, tgt, temp_dir):
        """One bad doc should not prevent other docs from being extracted."""
        jpg_bytes = SAMPLE_JPG.read_bytes()
        sub_dir = tmp_path / "src" / "sub-mixed"
        sub_dir.mkdir(parents=True)
        (sub_dir / "good.jpg").write_bytes(jpg_bytes)
        # ghost.pdf is intentionally not written
        (sub_dir / "desc.json").write_text(
            _make_desc_json(
                [
                    {
                        "stored_filename": "ghost.pdf",
                        "file_extension": ".pdf",
                        "mime_type": None,
                    },
                    {
                        "stored_filename": "good.jpg",
                        "file_extension": ".jpg",
                        "mime_type": "image/jpeg",
                    },
                ]
            )
        )

        submission = Submission(id="sub-mixed", source_path="sub-mixed")
        result = extract_pages(submission, src, tgt, temp_dir)
        records = result.page_records

        statuses = {r.doc_filename: r.status for r in records}
        assert statuses.get("ghost.pdf") == "failed"
        assert statuses.get("good.jpg") == "pending"
