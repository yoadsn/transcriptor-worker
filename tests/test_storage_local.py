"""Tests for LocalStorageBackend — all methods against a tmp_path fixture."""

from __future__ import annotations

import pytest

from transcriptor_worker.storage.local import LocalStorageBackend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage(tmp_path):
    """A LocalStorageBackend rooted at a fresh temp directory."""
    return LocalStorageBackend(str(tmp_path))


@pytest.fixture()
def populated(tmp_path):
    """A storage backend with a small pre-built directory tree.

    Layout::

        root/
          a/
            desc.json
            doc.pdf
          b/
            nested/
              desc.json
          file_at_root.txt
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "desc.json").write_text('{"hello": "world"}')
    (tmp_path / "a" / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "nested").mkdir()
    (tmp_path / "b" / "nested" / "desc.json").write_text('{"nested": true}')
    (tmp_path / "file_at_root.txt").write_text("root file")
    return LocalStorageBackend(str(tmp_path))


# ---------------------------------------------------------------------------
# list_prefixes
# ---------------------------------------------------------------------------


class TestListPrefixes:
    def test_empty_root(self, storage):
        assert storage.list_prefixes("") == []

    def test_top_level_dirs(self, populated):
        prefixes = populated.list_prefixes("")
        assert "a/" in prefixes
        assert "b/" in prefixes
        # Confirm count (only dirs, not the root txt file)
        assert len(prefixes) == 2

    def test_nested_prefix(self, populated):
        prefixes = populated.list_prefixes("b")
        assert "b/nested/" in prefixes

    def test_nonexistent_prefix_returns_empty(self, storage):
        assert storage.list_prefixes("does_not_exist") == []


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_files_at_root(self, populated):
        files = populated.list_files("")
        assert "file_at_root.txt" in files
        # Does not recurse into subdirs
        assert not any("desc.json" in f for f in files)

    def test_files_in_subfolder(self, populated):
        files = populated.list_files("a")
        assert "a/desc.json" in files
        assert "a/doc.pdf" in files

    def test_nonexistent_prefix_returns_empty(self, storage):
        assert storage.list_files("no/such/dir") == []


# ---------------------------------------------------------------------------
# walk
# ---------------------------------------------------------------------------


class TestWalk:
    def test_walk_finds_all_desc_json(self, populated):
        matches = populated.walk("", "desc.json")
        # Expect exactly two: a/desc.json and b/nested/desc.json
        assert len(matches) == 2
        paths_normalised = {m.replace("\\", "/") for m in matches}
        assert "a/desc.json" in paths_normalised
        assert "b/nested/desc.json" in paths_normalised

    def test_walk_scoped_to_subdir(self, populated):
        matches = populated.walk("a", "desc.json")
        assert len(matches) == 1
        assert matches[0].replace("\\", "/") == "a/desc.json"

    def test_walk_nonexistent_root_returns_empty(self, storage):
        assert storage.walk("no_such", "desc.json") == []

    def test_walk_no_matches_returns_empty(self, populated):
        assert populated.walk("", "missing_file.xyz") == []


# ---------------------------------------------------------------------------
# read_bytes / read_text
# ---------------------------------------------------------------------------


class TestRead:
    def test_read_bytes(self, storage, tmp_path):
        (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02")
        assert storage.read_bytes("data.bin") == b"\x00\x01\x02"

    def test_read_text(self, storage, tmp_path):
        (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
        assert storage.read_text("hello.txt") == "hello world"

    def test_read_nonexistent_raises(self, storage):
        with pytest.raises(FileNotFoundError):
            storage.read_bytes("no_such_file.bin")


# ---------------------------------------------------------------------------
# write_bytes / write_text
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_bytes_creates_file(self, storage, tmp_path):
        storage.write_bytes("out.bin", b"\xde\xad\xbe\xef")
        assert (tmp_path / "out.bin").read_bytes() == b"\xde\xad\xbe\xef"

    def test_write_text_creates_file(self, storage, tmp_path):
        storage.write_text("out.txt", "hello")
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"

    def test_write_creates_parent_dirs(self, storage, tmp_path):
        storage.write_bytes("sub/dir/file.bin", b"data")
        assert (tmp_path / "sub" / "dir" / "file.bin").read_bytes() == b"data"

    def test_write_overwrites_existing(self, storage, tmp_path):
        storage.write_bytes("file.bin", b"old")
        storage.write_bytes("file.bin", b"new")
        assert (tmp_path / "file.bin").read_bytes() == b"new"


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------


class TestExists:
    def test_existing_file(self, storage, tmp_path):
        (tmp_path / "present.txt").write_text("yes")
        assert storage.exists("present.txt") is True

    def test_missing_file(self, storage):
        assert storage.exists("absent.txt") is False

    def test_directory_not_file(self, storage, tmp_path):
        (tmp_path / "subdir").mkdir()
        # exists() should return True for directories too (pathlib .exists())
        assert storage.exists("subdir") is True


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_existing_file(self, storage, tmp_path):
        (tmp_path / "to_delete.txt").write_text("bye")
        storage.delete("to_delete.txt")
        assert not (tmp_path / "to_delete.txt").exists()

    def test_delete_nonexistent_is_noop(self, storage):
        # Must not raise
        storage.delete("does_not_exist.txt")

    def test_delete_then_write(self, storage, tmp_path):
        (tmp_path / "cycle.txt").write_text("v1")
        storage.delete("cycle.txt")
        storage.write_text("cycle.txt", "v2")
        assert (tmp_path / "cycle.txt").read_text() == "v2"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_bytes_round_trip(self, storage):
        data = bytes(range(256))
        storage.write_bytes("binary.bin", data)
        assert storage.read_bytes("binary.bin") == data

    def test_text_round_trip(self, storage):
        text = "Hello, world! \u05e9\u05dc\u05d5\u05dd"  # includes Hebrew
        storage.write_text("unicode.txt", text)
        assert storage.read_text("unicode.txt") == text
