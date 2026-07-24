"""Configuration loading from environment variables."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field


class ConfigError(ValueError):
    """Raised when required environment variables are missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Typed configuration for the transcriptor worker pipeline.

    Loaded from environment variables via :meth:`from_env`.
    """

    # Source storage
    source_storage_type: str  # "local" or "s3"
    source_storage_path: str  # local path or S3 URI (s3://bucket/prefix)
    source_aws_access_key_id: str | None
    source_aws_secret_access_key: str | None
    source_aws_region: str | None

    # Target storage
    target_storage_type: str  # "local" or "s3"
    target_storage_path: str  # local path or S3 URI (s3://bucket/prefix)
    target_aws_access_key_id: str | None
    target_aws_secret_access_key: str | None
    target_aws_region: str | None

    # Worker settings
    worker_parallelism: int

    # Testing / debugging
    max_submissions: int | None  # None = process all pending submissions
    force_reprocess: bool  # True = reprocess already-completed submissions
    force_reprocess_metadata: bool  # True = reprocess metadata.json only (no pages/manifests)
    backfill_raw_images: bool  # True = backfill raw AVIF images for existing target pages only

    # Surya detection thresholds (None = use library defaults)
    detector_text_threshold: float | None
    detector_blank_threshold: float | None

    # Submitter fingerprint salt (empty string = no salt)
    submitter_fingerprint_salt: str

    # Local temp directory for page images during a run
    temp_dir: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        """Construct a :class:`Config` from environment variables.

        Args:
            env: Optional dict to use instead of ``os.environ`` (for testing).

        Raises:
            ConfigError: If a required variable is missing or has an invalid value.
        """
        e = env if env is not None else dict(os.environ)

        def require(name: str) -> str:
            val = e.get(name, "").strip()
            if not val:
                raise ConfigError(f"Required environment variable {name!r} is not set.")
            return val

        def optional(name: str) -> str | None:
            val = e.get(name, "").strip()
            return val if val else None

        source_type = require("SOURCE_STORAGE_TYPE").lower()
        if source_type not in ("local", "s3"):
            raise ConfigError(
                f"SOURCE_STORAGE_TYPE must be 'local' or 's3', got {source_type!r}."
            )

        target_type = require("TARGET_STORAGE_TYPE").lower()
        if target_type not in ("local", "s3"):
            raise ConfigError(
                f"TARGET_STORAGE_TYPE must be 'local' or 's3', got {target_type!r}."
            )

        raw_parallelism = e.get("WORKER_PARALLELISM", "4").strip()
        try:
            parallelism = int(raw_parallelism)
            if parallelism < 1:
                raise ValueError
        except ValueError:
            raise ConfigError(
                f"WORKER_PARALLELISM must be a positive integer, got {raw_parallelism!r}."
            )

        def optional_int(name: str) -> int | None:
            val = e.get(name, "").strip()
            if not val:
                return None
            try:
                result = int(val)
                if result < 1:
                    raise ValueError
            except ValueError:
                raise ConfigError(
                    f"{name} must be a positive integer, got {val!r}."
                )
            return result

        def optional_bool(name: str) -> bool:
            val = e.get(name, "").strip().lower()
            return val in ("1", "true", "yes")

        def optional_float(name: str) -> float | None:
            val = e.get(name, "").strip()
            if not val:
                return None
            try:
                return float(val)
            except ValueError:
                raise ConfigError(
                    f"{name} must be a float, got {val!r}."
                )

        return cls(
            source_storage_type=source_type,
            source_storage_path=require("SOURCE_STORAGE_PATH"),
            source_aws_access_key_id=optional("SOURCE_AWS_ACCESS_KEY_ID"),
            source_aws_secret_access_key=optional("SOURCE_AWS_SECRET_ACCESS_KEY"),
            source_aws_region=optional("SOURCE_AWS_REGION"),
            target_storage_type=target_type,
            target_storage_path=require("TARGET_STORAGE_PATH"),
            target_aws_access_key_id=optional("TARGET_AWS_ACCESS_KEY_ID"),
            target_aws_secret_access_key=optional("TARGET_AWS_SECRET_ACCESS_KEY"),
            target_aws_region=optional("TARGET_AWS_REGION"),
            worker_parallelism=parallelism,
            max_submissions=optional_int("MAX_SUBMISSIONS"),
            force_reprocess=optional_bool("FORCE_REPROCESS"),
            force_reprocess_metadata=optional_bool("FORCE_REPROCESS_METADATA"),
            backfill_raw_images=optional_bool("BACKFILL_RAW_IMAGES"),
            detector_text_threshold=optional_float("DETECTOR_TEXT_THRESHOLD"),
            detector_blank_threshold=optional_float("DETECTOR_BLANK_THRESHOLD"),
            submitter_fingerprint_salt=e.get("SUBMITTER_FINGERPRINT_SALT", ""),
            temp_dir=e.get("TEMP_DIR", "").strip() or tempfile.gettempdir(),
        )
