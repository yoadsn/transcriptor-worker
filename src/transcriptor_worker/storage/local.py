"""Local filesystem storage backend."""

import logging
from pathlib import Path

from transcriptor_worker.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class LocalStorageBackend(StorageBackend):
    """StorageBackend implementation backed by the local filesystem.

    All paths are resolved relative to *root*.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()
        logger.debug("LocalStorageBackend initialised with root=%s", self._root)

    def _abs(self, path: str) -> Path:
        """Resolve *path* relative to root, ensuring it stays within root."""
        resolved = (self._root / path).resolve()
        # Safety check: don't allow traversal outside the root
        resolved.relative_to(self._root)
        return resolved

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_prefixes(self, prefix: str) -> list[str]:
        """List immediate child directories under *prefix*.

        Returns paths relative to root, with a trailing '/'.
        """
        base = self._abs(prefix) if prefix else self._root
        if not base.is_dir():
            return []
        results = []
        for entry in sorted(base.iterdir()):
            if entry.is_dir():
                rel = entry.relative_to(self._root)
                results.append(str(rel) + "/")
        logger.debug("list_prefixes(%s) -> %d entries", prefix, len(results))
        return results

    def list_files(self, prefix: str) -> list[str]:
        """List files directly under *prefix* (non-recursive).

        Returns paths relative to root.
        """
        base = self._abs(prefix) if prefix else self._root
        if not base.is_dir():
            return []
        results = []
        for entry in sorted(base.iterdir()):
            if entry.is_file():
                rel = entry.relative_to(self._root)
                results.append(str(rel))
        logger.debug("list_files(%s) -> %d entries", prefix, len(results))
        return results

    def walk(self, prefix: str, filename: str) -> list[str]:
        """Recursively find all paths ending with *filename* under *prefix*.

        Returns paths relative to root.
        """
        base = self._abs(prefix) if prefix else self._root
        if not base.exists():
            return []
        results = []
        for match in sorted(base.rglob(filename)):
            if match.is_file():
                rel = match.relative_to(self._root)
                results.append(str(rel))
        logger.debug("walk(%s, %s) -> %d matches", prefix, filename, len(results))
        return results

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_bytes(self, path: str) -> bytes:
        abs_path = self._abs(path)
        logger.debug("read_bytes(%s)", path)
        return abs_path.read_bytes()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        abs_path = self._abs(path)
        logger.debug("read_text(%s)", path)
        return abs_path.read_text(encoding=encoding)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write_bytes(self, path: str, data: bytes) -> None:
        abs_path = self._abs(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("write_bytes(%s, %d bytes)", path, len(data))
        abs_path.write_bytes(data)

    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        abs_path = self._abs(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("write_text(%s, %d chars)", path, len(data))
        abs_path.write_text(data, encoding=encoding)

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        try:
            return self._abs(path).exists()
        except ValueError:
            return False

    def delete(self, path: str) -> None:
        """Delete the file at *path*. No-op if the file does not exist."""
        try:
            abs_path = self._abs(path)
            abs_path.unlink(missing_ok=True)
            logger.debug("delete(%s)", path)
        except ValueError:
            pass
