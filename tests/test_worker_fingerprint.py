"""Tests for submitter_fingerprint in worker.py — Stage 3 metadata extraction."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from transcriptor_worker.models import Submission
import transcriptor_worker.worker as worker_mod
from transcriptor_worker.worker import StorageConfig, process_submission


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER = "transcriptor_worker.worker"


def _submission(id: str = "sub-1", source_path: str = "src/sub-1") -> Submission:
    return Submission(id=id, source_path=source_path)


def _desc_json(
    form_metadata: dict | None = None,
    user_email: str | None = None,
) -> bytes:
    """Build a desc.json bytes blob."""
    d: dict = {}
    if form_metadata is not None:
        d["form_metadata"] = form_metadata
    if user_email is not None:
        d["user"] = {"email": user_email}
    return json.dumps(d).encode()


def _expected_fingerprint(salt: str, email: str) -> str:
    return hashlib.sha256((salt + email).encode()).hexdigest()


def _run_metadata_stage(
    desc_bytes: bytes,
    salt: str | None = None,
) -> dict:
    """Run process_submission with heavy mocking, return the metadata dict
    that was written to target storage.
    """
    sub = _submission()
    source_cfg = StorageConfig(storage_type="local", storage_path="/src")
    target_cfg = StorageConfig(storage_type="local", storage_path="/tgt")

    # Mock storage backends
    storage = MagicMock()
    storage.read_bytes.return_value = desc_bytes

    written_metadata: dict = {}

    def capture_write(path: str, content: str) -> None:
        if path.endswith("metadata.json"):
            written_metadata.update(json.loads(content))

    storage.write_text.side_effect = capture_write

    # Mock _build_backend to return our mock storage
    # Mock extract_pages to return empty page records
    # Mock process_page_lines — should not be called with 0 pages
    pages_result = MagicMock()
    pages_result.page_records = []
    pages_result.transforms = {}

    with (
        patch(f"{_WORKER}._build_backend") as mock_build,
        patch(f"{_WORKER}.extract_pages", return_value=pages_result),
        patch(f"{_WORKER}.process_page_lines") as mock_lines,
        patch.object(worker_mod, "_submitter_fingerprint_salt", salt if salt is not None else ""),
    ):
        mock_build.return_value = (storage, "")
        process_submission(sub, source_cfg, target_cfg, "/tmp")
        assert not mock_lines.called, "process_page_lines should not be called with 0 pages"

    return written_metadata


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubmitterFingerprint:
    def test_fingerprint_added_with_email(self):
        desc = _desc_json(form_metadata={"key": "val"}, user_email="User@Example.COM")
        meta = _run_metadata_stage(desc, salt="")
        assert meta["submitter_fingerprint"] == _expected_fingerprint("", "user@example.com")
        assert meta["key"] == "val"

    def test_email_is_normalized(self):
        desc = _desc_json(user_email="  Test@Domain.org  ")
        meta = _run_metadata_stage(desc, salt="")
        assert meta["submitter_fingerprint"] == _expected_fingerprint("", "test@domain.org")

    def test_email_is_lowercased(self):
        desc = _desc_json(user_email="ALICE@HOST.COM")
        meta = _run_metadata_stage(desc, salt="")
        assert meta["submitter_fingerprint"] == _expected_fingerprint("", "alice@host.com")

    def test_no_fingerprint_when_email_missing(self):
        desc = _desc_json(form_metadata={"foo": 1})
        meta = _run_metadata_stage(desc, salt="")
        assert "submitter_fingerprint" not in meta
        assert meta["foo"] == 1

    def test_no_fingerprint_when_user_key_missing(self):
        desc = _desc_json(form_metadata={"foo": 1})
        meta = _run_metadata_stage(desc, salt="")
        assert "submitter_fingerprint" not in meta

    def test_no_fingerprint_when_email_none(self):
        """user key exists but email is absent."""
        desc = json.dumps({"user": {"name": "nobody"}, "form_metadata": {}}).encode()
        meta = _run_metadata_stage(desc, salt="")
        assert "submitter_fingerprint" not in meta

    def test_no_fingerprint_when_email_empty_string(self):
        desc = _desc_json(user_email="")
        meta = _run_metadata_stage(desc, salt="")
        assert "submitter_fingerprint" not in meta

    def test_fingerprint_for_whitespace_only_email(self):
        """Whitespace-only email is truthy so a fingerprint IS produced (of empty string)."""
        desc = _desc_json(user_email="   ")
        meta = _run_metadata_stage(desc, salt="")
        assert meta["submitter_fingerprint"] == _expected_fingerprint("", "")

    def test_fingerprint_changes_with_salt(self):
        desc = _desc_json(user_email="alice@host.com")
        meta_no_salt = _run_metadata_stage(desc, salt="")
        meta_with_salt = _run_metadata_stage(desc, salt="my-secret-salt")
        assert meta_no_salt["submitter_fingerprint"] != meta_with_salt["submitter_fingerprint"]
        assert meta_with_salt["submitter_fingerprint"] == _expected_fingerprint(
            "my-secret-salt", "alice@host.com"
        )

    def test_fingerprint_is_deterministic(self):
        desc = _desc_json(user_email="bob@test.com")
        meta1 = _run_metadata_stage(desc, salt="s")
        meta2 = _run_metadata_stage(desc, salt="s")
        assert meta1["submitter_fingerprint"] == meta2["submitter_fingerprint"]

    def test_fingerprint_differs_per_email(self):
        desc_a = _desc_json(user_email="a@b.com")
        desc_b = _desc_json(user_email="c@d.com")
        meta_a = _run_metadata_stage(desc_a, salt="s")
        meta_b = _run_metadata_stage(desc_b, salt="s")
        assert meta_a["submitter_fingerprint"] != meta_b["submitter_fingerprint"]

    def test_fingerprint_is_sha256_hex(self):
        desc = _desc_json(user_email="x@y.com")
        meta = _run_metadata_stage(desc, salt="")
        fp = meta["submitter_fingerprint"]
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_form_metadata_preserved(self):
        desc = _desc_json(
            form_metadata={"a": 1, "b": "two"},
            user_email="z@z.com",
        )
        meta = _run_metadata_stage(desc, salt="salt")
        assert meta["a"] == 1
        assert meta["b"] == "two"
        assert "submitter_fingerprint" in meta

    def test_empty_form_metadata_gets_fingerprint(self):
        desc = _desc_json(form_metadata={}, user_email="z@z.com")
        meta = _run_metadata_stage(desc, salt="")
        assert meta == {"submitter_fingerprint": _expected_fingerprint("", "z@z.com")}
