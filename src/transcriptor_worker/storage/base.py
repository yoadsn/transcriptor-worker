"""Abstract base class / protocol for storage backends."""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Protocol for storage backends.

    All paths are strings. For S3, paths are keys (without a leading slash).
    For local storage, paths are filesystem paths (absolute or relative to a root).
    """

    @abstractmethod
    def list_prefixes(self, prefix: str) -> list[str]:
        """List immediate child 'directories' under *prefix*.

        Returns a list of prefix strings (each ending with '/').
        """

    @abstractmethod
    def list_files(self, prefix: str) -> list[str]:
        """List files directly under *prefix* (non-recursive).

        Returns file paths (not prefixes/directories).
        """

    @abstractmethod
    def walk(self, prefix: str, filename: str) -> list[str]:
        """Recursively find all paths ending with *filename* under *prefix*.

        Returns full paths to each matching file.
        """

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        """Read the file at *path* and return its contents as bytes."""

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read the file at *path* and return its contents as a string."""

    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* bytes to *path*, creating parent directories as needed."""

    @abstractmethod
    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        """Write *data* string to *path*, creating parent directories as needed."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Return True if *path* exists in this storage backend."""

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete the file at *path*.

        Implementations should be idempotent: deleting a non-existent path
        must not raise an error.
        """
