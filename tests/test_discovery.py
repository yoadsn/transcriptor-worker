"""Tests for discovery.py — finding submissions from a fixture directory tree."""

from __future__ import annotations

import pytest

from transcriptor_worker.discovery import discover_submissions
from transcriptor_worker.storage.local import LocalStorageBackend


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_tree(tmp_path, structure: dict) -> None:
    """Recursively create a directory structure.

    *structure* is a dict where:
    - str keys with dict values -> directories
    - str keys with bytes/str values -> files (bytes = binary, str = text)
    - str keys with None values -> empty files
    """
    for name, content in structure.items():
        path = tmp_path / name
        if isinstance(content, dict):
            path.mkdir(parents=True, exist_ok=True)
            _make_tree(path, content)
        elif isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content)
        else:
            path.touch()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiscoverSubmissions:
    def test_single_flat_submission(self, tmp_path):
        """Simplest case: one submission at depth 1."""
        _make_tree(tmp_path, {
            "sub-001": {
                "desc.json": '{"files": []}',
                "image.jpg": b"\xff\xd8",
            }
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        assert len(submissions) == 1
        assert submissions[0].id == "sub-001"

    def test_multiple_flat_submissions(self, tmp_path):
        _make_tree(tmp_path, {
            "a": {"desc.json": "{}"},
            "b": {"desc.json": "{}"},
            "c": {"desc.json": "{}"},
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        ids = {s.id for s in submissions}
        assert ids == {"a", "b", "c"}

    def test_nested_submissions(self, tmp_path):
        """Submissions can be nested at arbitrary depth."""
        _make_tree(tmp_path, {
            "users": {
                "user-1": {
                    "2024": {
                        "batch-42": {
                            "desc.json": "{}",
                        }
                    }
                },
                "user-2": {
                    "sub-99": {
                        "desc.json": "{}",
                    }
                }
            }
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        ids = {s.id for s in submissions}
        assert ids == {"batch-42", "sub-99"}

    def test_source_path_is_parent_of_desc_json(self, tmp_path):
        _make_tree(tmp_path, {
            "uploads": {
                "some-id": {
                    "desc.json": "{}",
                }
            }
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        assert len(submissions) == 1
        s = submissions[0]
        assert s.id == "some-id"
        # source_path should end with the submission folder, not desc.json
        assert s.source_path.replace("\\", "/").endswith("some-id")
        assert "desc.json" not in s.source_path

    def test_empty_tree_returns_empty_list(self, tmp_path):
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        assert submissions == []

    def test_no_desc_json_returns_empty_list(self, tmp_path):
        _make_tree(tmp_path, {
            "sub-001": {
                "image.jpg": b"\xff\xd8",
            }
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        assert submissions == []

    def test_scoped_prefix_limits_discovery(self, tmp_path):
        """Discovery scoped to a prefix should not find submissions outside it."""
        _make_tree(tmp_path, {
            "bucket-a": {
                "sub-in-a": {"desc.json": "{}"},
            },
            "bucket-b": {
                "sub-in-b": {"desc.json": "{}"},
            },
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "bucket-a")
        ids = {s.id for s in submissions}
        assert "sub-in-a" in ids
        assert "sub-in-b" not in ids

    def test_extra_files_in_submission_folder_ignored(self, tmp_path):
        """Non-desc.json files in the submission folder don't create extra submissions."""
        _make_tree(tmp_path, {
            "sub-001": {
                "desc.json": "{}",
                "scan.pdf": b"%PDF",
                "notes.txt": "notes",
                "thumbnail.jpg": b"\xff\xd8",
            }
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        assert len(submissions) == 1
        assert submissions[0].id == "sub-001"

    def test_mixed_depth(self, tmp_path):
        """Submissions at different depths are all discovered."""
        _make_tree(tmp_path, {
            "shallow": {"desc.json": "{}"},
            "deep": {
                "level1": {
                    "level2": {
                        "deep-sub": {"desc.json": "{}"},
                    }
                }
            },
        })
        storage = LocalStorageBackend(str(tmp_path))
        submissions = discover_submissions(storage, "")
        ids = {s.id for s in submissions}
        assert "shallow" in ids
        assert "deep-sub" in ids
