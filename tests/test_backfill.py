"""Tests for backfill.py — BACKFILL_RAW_IMAGES mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from transcriptor_worker.backfill import run_backfill
from transcriptor_worker.config import Config
from transcriptor_worker.manifest import load_pages_csv, save_pages_csv
from transcriptor_worker.models import PageRecord
from transcriptor_worker.storage.local import LocalStorageBackend

FIXTURES = Path(__file__).parent / "fixtures"
TWO_PAGE_PDF = FIXTURES / "two_page.pdf"
SAMPLE_JPG = FIXTURES / "sample.jpg"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def src(tmp_path):
    return LocalStorageBackend(str(tmp_path / "src"))


@pytest.fixture()
def tgt(tmp_path):
    return LocalStorageBackend(str(tmp_path / "tgt"))


def _make_config(tmp_path, max_submissions: int | None = None) -> Config:
    return Config.from_env(
        {
            "SOURCE_STORAGE_TYPE": "local",
            "SOURCE_STORAGE_PATH": str(tmp_path / "src"),
            "TARGET_STORAGE_TYPE": "local",
            "TARGET_STORAGE_PATH": str(tmp_path / "tgt"),
            "BACKFILL_RAW_IMAGES": "true",
            **({"MAX_SUBMISSIONS": str(max_submissions)} if max_submissions else {}),
        }
    )


def _make_desc_json(files: list[dict]) -> str:
    return json.dumps({"version": "1", "user": "test", "files": files})


def _write_source_submission(
    tmp_path: Path, sub_id: str, files: dict[str, bytes], desc_files: list[dict]
) -> None:
    sub_dir = tmp_path / "src" / sub_id
    sub_dir.mkdir(parents=True)
    for filename, data in files.items():
        (sub_dir / filename).write_bytes(data)
    (sub_dir / "desc.json").write_text(_make_desc_json(desc_files))


def _write_target_page(
    tmp_path: Path,
    sub_id: str,
    image_filename: str,
    lines_filename: str,
    image_width: int = 100,
    image_height: int = 80,
) -> None:
    """Write a minimal derived image + lines.json for a page in target."""
    sub_dir = tmp_path / "tgt" / sub_id
    sub_dir.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (image_width, image_height), color="blue")
    img.save(sub_dir / image_filename, format="JPEG")
    payload = {
        "submission_id": sub_id,
        "image_filename": image_filename,
        "image_width": image_width,
        "image_height": image_height,
        "raw_image_filename": "",
        "raw_image_width": 0,
        "raw_image_height": 0,
        "lines": [],
    }
    (sub_dir / lines_filename).write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Happy-path: image passthrough submission
# ---------------------------------------------------------------------------


class TestBackfillImagePage:
    def test_backfills_raw_image_and_pages_csv(self, tmp_path, src, tgt):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        _write_source_submission(
            tmp_path,
            "sub1",
            {"photo.jpg": jpg_bytes},
            [{"stored_filename": "photo.jpg", "file_extension": ".jpg", "mime_type": "image/jpeg"}],
        )
        _write_target_page(tmp_path, "sub1", "photo_p1.jpg", "photo_p1.jpg.json")

        records = [
            PageRecord(
                submission_id="sub1",
                doc_filename="photo.jpg",
                page_number=1,
                status="completed",
                image_filename="photo_p1.jpg",
                lines_filename="photo_p1.jpg.json",
            )
        ]
        save_pages_csv(records, tgt, "pages.csv")

        config = _make_config(tmp_path)
        run_backfill(config)

        assert tgt.exists("sub1/photo_p1.avif")

        loaded = load_pages_csv(tgt, "pages.csv")
        assert len(loaded) == 1
        assert loaded[0].raw_image_filename == "photo_p1.avif"
        assert loaded[0].raw_image_width > 0
        assert loaded[0].raw_image_height > 0
        # Untouched fields preserved
        assert loaded[0].image_filename == "photo_p1.jpg"
        assert loaded[0].lines_filename == "photo_p1.jpg.json"
        assert loaded[0].status == "completed"

    def test_patches_lines_json(self, tmp_path, src, tgt):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        _write_source_submission(
            tmp_path,
            "sub1",
            {"photo.jpg": jpg_bytes},
            [{"stored_filename": "photo.jpg", "file_extension": ".jpg", "mime_type": "image/jpeg"}],
        )
        _write_target_page(tmp_path, "sub1", "photo_p1.jpg", "photo_p1.jpg.json")

        records = [
            PageRecord(
                submission_id="sub1",
                doc_filename="photo.jpg",
                page_number=1,
                status="completed",
                image_filename="photo_p1.jpg",
                lines_filename="photo_p1.jpg.json",
            )
        ]
        save_pages_csv(records, tgt, "pages.csv")

        run_backfill(_make_config(tmp_path))

        payload = json.loads(tgt.read_text("sub1/photo_p1.jpg.json"))
        assert payload["raw_image_filename"] == "photo_p1.avif"
        assert payload["raw_image_width"] > 0
        assert payload["raw_image_height"] > 0
        # Existing fields preserved
        assert payload["image_filename"] == "photo_p1.jpg"


# ---------------------------------------------------------------------------
# PDF submission (multi-page)
# ---------------------------------------------------------------------------


class TestBackfillPdfPages:
    def test_backfills_all_pages_of_pdf(self, tmp_path, src, tgt):
        pdf_bytes = TWO_PAGE_PDF.read_bytes()
        _write_source_submission(
            tmp_path,
            "sub1",
            {"doc.pdf": pdf_bytes},
            [{"stored_filename": "doc.pdf", "file_extension": ".pdf", "mime_type": None}],
        )
        _write_target_page(tmp_path, "sub1", "doc_p1.jpg", "doc_p1.jpg.json")
        _write_target_page(tmp_path, "sub1", "doc_p2.jpg", "doc_p2.jpg.json")

        records = [
            PageRecord(
                submission_id="sub1",
                doc_filename="doc.pdf",
                page_number=i,
                status="completed",
                image_filename=f"doc_p{i}.jpg",
                lines_filename=f"doc_p{i}.jpg.json",
            )
            for i in (1, 2)
        ]
        save_pages_csv(records, tgt, "pages.csv")

        run_backfill(_make_config(tmp_path))

        assert tgt.exists("sub1/doc_p1.avif")
        assert tgt.exists("sub1/doc_p2.avif")

        loaded = {r.page_number: r for r in load_pages_csv(tgt, "pages.csv")}
        assert loaded[1].raw_image_filename == "doc_p1.avif"
        assert loaded[2].raw_image_filename == "doc_p2.avif"


# ---------------------------------------------------------------------------
# Skip / no-op cases
# ---------------------------------------------------------------------------


class TestBackfillSkipsAndNoOps:
    def test_pages_with_raw_image_already_set_are_untouched(self, tmp_path, src, tgt):
        _write_target_page(tmp_path, "sub1", "photo_p1.jpg", "photo_p1.jpg.json")
        records = [
            PageRecord(
                submission_id="sub1",
                doc_filename="photo.jpg",
                page_number=1,
                status="completed",
                image_filename="photo_p1.jpg",
                lines_filename="photo_p1.jpg.json",
                raw_image_filename="photo_p1.avif",
                raw_image_width=500,
                raw_image_height=400,
            )
        ]
        save_pages_csv(records, tgt, "pages.csv")

        # No source submission exists at all — if backfill tried to touch
        # this page it would fail/log a warning; assert it's simply a no-op.
        run_backfill(_make_config(tmp_path))

        loaded = load_pages_csv(tgt, "pages.csv")
        assert loaded[0].raw_image_filename == "photo_p1.avif"
        assert loaded[0].raw_image_width == 500
        assert loaded[0].raw_image_height == 400

    def test_failed_pages_are_not_backfilled(self, tmp_path, src, tgt):
        records = [
            PageRecord(
                submission_id="sub1",
                doc_filename="photo.jpg",
                page_number=1,
                status="failed",
                error="boom",
                image_filename="",
                lines_filename="",
            )
        ]
        save_pages_csv(records, tgt, "pages.csv")

        run_backfill(_make_config(tmp_path))

        loaded = load_pages_csv(tgt, "pages.csv")
        assert loaded[0].raw_image_filename == ""

    def test_empty_pages_csv_is_a_no_op(self, tmp_path, src, tgt):
        save_pages_csv([], tgt, "pages.csv")
        # Should not raise.
        run_backfill(_make_config(tmp_path))
        assert load_pages_csv(tgt, "pages.csv") == []

    def test_missing_pages_csv_is_a_no_op(self, tmp_path, src, tgt):
        # Should not raise even though pages.csv does not exist yet.
        run_backfill(_make_config(tmp_path))

    def test_does_not_process_new_source_submissions(self, tmp_path, src, tgt):
        """A submission that exists only in source (never processed into
        target's pages.csv) must not be touched or added by backfill."""
        jpg_bytes = SAMPLE_JPG.read_bytes()
        # sub1 is already in target/pages.csv and needs backfill.
        _write_source_submission(
            tmp_path,
            "sub1",
            {"photo.jpg": jpg_bytes},
            [{"stored_filename": "photo.jpg", "file_extension": ".jpg", "mime_type": "image/jpeg"}],
        )
        _write_target_page(tmp_path, "sub1", "photo_p1.jpg", "photo_p1.jpg.json")
        records = [
            PageRecord(
                submission_id="sub1",
                doc_filename="photo.jpg",
                page_number=1,
                status="completed",
                image_filename="photo_p1.jpg",
                lines_filename="photo_p1.jpg.json",
            )
        ]
        save_pages_csv(records, tgt, "pages.csv")

        # sub2 exists ONLY in source — never processed, not in pages.csv.
        _write_source_submission(
            tmp_path,
            "sub2",
            {"other.jpg": jpg_bytes},
            [{"stored_filename": "other.jpg", "file_extension": ".jpg", "mime_type": "image/jpeg"}],
        )

        run_backfill(_make_config(tmp_path))

        # sub1 got backfilled...
        assert tgt.exists("sub1/photo_p1.avif")
        # ...but sub2 was never touched — no output directory for it at all.
        assert not tgt.exists("sub2")
        loaded = load_pages_csv(tgt, "pages.csv")
        assert len(loaded) == 1
        assert loaded[0].submission_id == "sub1"


# ---------------------------------------------------------------------------
# MAX_SUBMISSIONS limiting
# ---------------------------------------------------------------------------


class TestBackfillMaxSubmissions:
    def test_limits_number_of_submissions_backfilled(self, tmp_path, src, tgt):
        jpg_bytes = SAMPLE_JPG.read_bytes()
        for sub_id in ("subA", "subB"):
            _write_source_submission(
                tmp_path,
                sub_id,
                {"photo.jpg": jpg_bytes},
                [{"stored_filename": "photo.jpg", "file_extension": ".jpg", "mime_type": "image/jpeg"}],
            )
            _write_target_page(tmp_path, sub_id, "photo_p1.jpg", "photo_p1.jpg.json")

        records = [
            PageRecord(
                submission_id=sub_id,
                doc_filename="photo.jpg",
                page_number=1,
                status="completed",
                image_filename="photo_p1.jpg",
                lines_filename="photo_p1.jpg.json",
            )
            for sub_id in ("subA", "subB")
        ]
        save_pages_csv(records, tgt, "pages.csv")

        run_backfill(_make_config(tmp_path, max_submissions=1))

        loaded = {r.submission_id: r for r in load_pages_csv(tgt, "pages.csv")}
        backfilled = [sid for sid, r in loaded.items() if r.raw_image_filename]
        assert len(backfilled) == 1
        # "subA" sorts first.
        assert backfilled == ["subA"]
