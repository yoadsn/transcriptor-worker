"""S3 storage backend."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from transcriptor_worker.storage.base import StorageBackend
from transcriptor_worker.storage.retry import with_retry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class S3StorageBackend(StorageBackend):
    """StorageBackend implementation backed by Amazon S3.

    All paths are S3 object keys (no leading slash).  The *bucket* is fixed
    at construction time.

    AWS credentials are supplied explicitly rather than relying on the
    environment so that source and target can use different credentials.
    """

    def __init__(
        self,
        bucket: str,
        *,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        session = boto3.Session(
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=aws_region,
        )
        self._s3 = session.client("s3", endpoint_url=endpoint_url)
        logger.debug("S3StorageBackend initialised with bucket=%s", bucket)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_prefixes(self, prefix: str) -> list[str]:
        """List immediate child 'directories' (common prefixes) under *prefix*."""
        # Ensure prefix ends with '/' if non-empty
        norm_prefix = (prefix.rstrip("/") + "/") if prefix else ""
        paginator = self._s3.get_paginator("list_objects_v2")
        results: list[str] = []
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=norm_prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes") or []:
                results.append(cp["Prefix"])
        logger.debug("list_prefixes(%s) -> %d entries", prefix, len(results))
        return results

    def list_files(self, prefix: str) -> list[str]:
        """List files directly under *prefix* (non-recursive)."""
        norm_prefix = (prefix.rstrip("/") + "/") if prefix else ""
        paginator = self._s3.get_paginator("list_objects_v2")
        results: list[str] = []
        for page in paginator.paginate(
            Bucket=self._bucket, Prefix=norm_prefix, Delimiter="/"
        ):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                # Skip the 'directory' placeholder itself
                if key != norm_prefix:
                    results.append(key)
        logger.debug("list_files(%s) -> %d entries", prefix, len(results))
        return results

    def walk(self, prefix: str, filename: str) -> list[str]:
        """Recursively find all keys ending with *filename* under *prefix*."""
        norm_prefix = (prefix.rstrip("/") + "/") if prefix else ""
        paginator = self._s3.get_paginator("list_objects_v2")
        results: list[str] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=norm_prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if key.endswith("/" + filename) or key == filename:
                    results.append(key)
        logger.debug("walk(%s, %s) -> %d matches", prefix, filename, len(results))
        return results

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def read_bytes(self, path: str) -> bytes:
        logger.debug("read_bytes(%s)", path)
        response = self._s3.get_object(Bucket=self._bucket, Key=path)
        return response["Body"].read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    # ------------------------------------------------------------------
    # Writing (with retry)
    # ------------------------------------------------------------------

    def write_bytes(self, path: str, data: bytes) -> None:
        logger.debug("write_bytes(%s, %d bytes)", path, len(data))
        with_retry(
            lambda: self._s3.put_object(Bucket=self._bucket, Key=path, Body=data),
            operation_label=f"s3.put_object({path})",
        )

    def write_text(self, path: str, data: str, encoding: str = "utf-8") -> None:
        self.write_bytes(path, data.encode(encoding))

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    def exists(self, path: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=path)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def delete(self, path: str) -> None:
        """Delete an S3 object at *path*. Idempotent (no-op if absent)."""
        logger.debug("delete(%s)", path)
        # S3 delete_object is idempotent — no error if key doesn't exist.
        with_retry(
            lambda: self._s3.delete_object(Bucket=self._bucket, Key=path),
            operation_label=f"s3.delete_object({path})",
        )
