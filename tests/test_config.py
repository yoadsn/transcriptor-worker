"""Tests for config.py — env var parsing, validation, and defaults."""

from __future__ import annotations

import pytest

from transcriptor_worker.config import Config, ConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_LOCAL = {
    "SOURCE_STORAGE_TYPE": "local",
    "SOURCE_STORAGE_PATH": "/data/src",
    "TARGET_STORAGE_TYPE": "local",
    "TARGET_STORAGE_PATH": "/data/tgt",
}


def _env(**overrides: str) -> dict[str, str]:
    """Build a minimal valid env dict, applying *overrides*."""
    base = dict(_REQUIRED_LOCAL)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestConfigHappyPath:
    def test_minimal_local(self):
        cfg = Config.from_env(_env())
        assert cfg.source_storage_type == "local"
        assert cfg.source_storage_path == "/data/src"
        assert cfg.target_storage_type == "local"
        assert cfg.target_storage_path == "/data/tgt"

    def test_worker_parallelism_default(self):
        cfg = Config.from_env(_env())
        assert cfg.worker_parallelism == 4

    def test_worker_parallelism_custom(self):
        cfg = Config.from_env(_env(WORKER_PARALLELISM="8"))
        assert cfg.worker_parallelism == 8

    def test_storage_type_uppercase_normalised(self):
        cfg = Config.from_env(_env(SOURCE_STORAGE_TYPE="LOCAL", TARGET_STORAGE_TYPE="S3",
                                   TARGET_STORAGE_PATH="s3://bucket/prefix",
                                   TARGET_AWS_ACCESS_KEY_ID="AKIA",
                                   TARGET_AWS_SECRET_ACCESS_KEY="SECRET",
                                   TARGET_AWS_REGION="us-east-1"))
        assert cfg.source_storage_type == "local"
        assert cfg.target_storage_type == "s3"

    def test_optional_aws_fields_absent(self):
        cfg = Config.from_env(_env())
        assert cfg.source_aws_access_key_id is None
        assert cfg.source_aws_secret_access_key is None
        assert cfg.source_aws_region is None
        assert cfg.target_aws_access_key_id is None

    def test_optional_aws_fields_present(self):
        cfg = Config.from_env(
            _env(
                SOURCE_AWS_ACCESS_KEY_ID="AKID",
                SOURCE_AWS_SECRET_ACCESS_KEY="SECRET",
                SOURCE_AWS_REGION="eu-west-1",
            )
        )
        assert cfg.source_aws_access_key_id == "AKID"
        assert cfg.source_aws_secret_access_key == "SECRET"
        assert cfg.source_aws_region == "eu-west-1"

    def test_detector_thresholds_none_by_default(self):
        cfg = Config.from_env(_env())
        assert cfg.detector_text_threshold is None
        assert cfg.detector_blank_threshold is None

    def test_detector_thresholds_parsed(self):
        cfg = Config.from_env(
            _env(DETECTOR_TEXT_THRESHOLD="0.5", DETECTOR_BLANK_THRESHOLD="0.2")
        )
        assert cfg.detector_text_threshold == pytest.approx(0.5)
        assert cfg.detector_blank_threshold == pytest.approx(0.2)

    def test_temp_dir_default_is_system_temp(self):
        import tempfile
        cfg = Config.from_env(_env())
        assert cfg.temp_dir == tempfile.gettempdir()

    def test_temp_dir_override(self):
        cfg = Config.from_env(_env(TEMP_DIR="/custom/tmp"))
        assert cfg.temp_dir == "/custom/tmp"

    def test_config_is_frozen(self):
        cfg = Config.from_env(_env())
        with pytest.raises((AttributeError, TypeError)):
            cfg.worker_parallelism = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------


class TestConfigErrors:
    def test_missing_source_storage_type(self):
        env = _env()
        del env["SOURCE_STORAGE_TYPE"]
        with pytest.raises(ConfigError, match="SOURCE_STORAGE_TYPE"):
            Config.from_env(env)

    def test_missing_source_storage_path(self):
        env = _env()
        del env["SOURCE_STORAGE_PATH"]
        with pytest.raises(ConfigError, match="SOURCE_STORAGE_PATH"):
            Config.from_env(env)

    def test_missing_target_storage_type(self):
        env = _env()
        del env["TARGET_STORAGE_TYPE"]
        with pytest.raises(ConfigError, match="TARGET_STORAGE_TYPE"):
            Config.from_env(env)

    def test_missing_target_storage_path(self):
        env = _env()
        del env["TARGET_STORAGE_PATH"]
        with pytest.raises(ConfigError, match="TARGET_STORAGE_PATH"):
            Config.from_env(env)

    def test_invalid_source_storage_type(self):
        with pytest.raises(ConfigError, match="SOURCE_STORAGE_TYPE"):
            Config.from_env(_env(SOURCE_STORAGE_TYPE="ftp"))

    def test_invalid_target_storage_type(self):
        with pytest.raises(ConfigError, match="TARGET_STORAGE_TYPE"):
            Config.from_env(_env(TARGET_STORAGE_TYPE="azure"))

    def test_invalid_worker_parallelism_string(self):
        with pytest.raises(ConfigError, match="WORKER_PARALLELISM"):
            Config.from_env(_env(WORKER_PARALLELISM="four"))

    def test_invalid_worker_parallelism_zero(self):
        with pytest.raises(ConfigError, match="WORKER_PARALLELISM"):
            Config.from_env(_env(WORKER_PARALLELISM="0"))

    def test_invalid_worker_parallelism_negative(self):
        with pytest.raises(ConfigError, match="WORKER_PARALLELISM"):
            Config.from_env(_env(WORKER_PARALLELISM="-1"))

    def test_invalid_text_threshold(self):
        with pytest.raises(ConfigError, match="DETECTOR_TEXT_THRESHOLD"):
            Config.from_env(_env(DETECTOR_TEXT_THRESHOLD="not-a-float"))

    def test_invalid_blank_threshold(self):
        with pytest.raises(ConfigError, match="DETECTOR_BLANK_THRESHOLD"):
            Config.from_env(_env(DETECTOR_BLANK_THRESHOLD="bad"))

    def test_empty_string_treated_as_missing(self):
        """Whitespace-only values are treated the same as missing."""
        with pytest.raises(ConfigError, match="SOURCE_STORAGE_TYPE"):
            Config.from_env(_env(SOURCE_STORAGE_TYPE="   "))


# ---------------------------------------------------------------------------
# MAX_SUBMISSIONS tests
# ---------------------------------------------------------------------------


class TestMaxSubmissions:
    def test_absent_gives_none(self):
        cfg = Config.from_env(_env())
        assert cfg.max_submissions is None

    def test_valid_value_parsed(self):
        cfg = Config.from_env(_env(MAX_SUBMISSIONS="5"))
        assert cfg.max_submissions == 5

    def test_zero_raises_config_error(self):
        with pytest.raises(ConfigError, match="MAX_SUBMISSIONS"):
            Config.from_env(_env(MAX_SUBMISSIONS="0"))

    def test_negative_raises_config_error(self):
        with pytest.raises(ConfigError, match="MAX_SUBMISSIONS"):
            Config.from_env(_env(MAX_SUBMISSIONS="-1"))

    def test_non_integer_raises_config_error(self):
        with pytest.raises(ConfigError, match="MAX_SUBMISSIONS"):
            Config.from_env(_env(MAX_SUBMISSIONS="abc"))


# ---------------------------------------------------------------------------
# FORCE_REPROCESS tests
# ---------------------------------------------------------------------------


class TestForceReprocess:
    def test_default_is_false(self):
        cfg = Config.from_env(_env())
        assert cfg.force_reprocess is False

    def test_true_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS="true"))
        assert cfg.force_reprocess is True

    def test_1_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS="1"))
        assert cfg.force_reprocess is True

    def test_yes_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS="yes"))
        assert cfg.force_reprocess is True

    def test_false_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS="false"))
        assert cfg.force_reprocess is False

    def test_empty_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS=""))
        assert cfg.force_reprocess is False

    def test_random_string(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS="anything"))
        assert cfg.force_reprocess is False


# ---------------------------------------------------------------------------
# FORCE_REPROCESS_METADATA tests
# ---------------------------------------------------------------------------


class TestForceReprocessMetadata:
    def test_default_is_false(self):
        cfg = Config.from_env(_env())
        assert cfg.force_reprocess_metadata is False

    def test_true_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS_METADATA="true"))
        assert cfg.force_reprocess_metadata is True

    def test_1_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS_METADATA="1"))
        assert cfg.force_reprocess_metadata is True

    def test_yes_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS_METADATA="yes"))
        assert cfg.force_reprocess_metadata is True

    def test_false_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS_METADATA="false"))
        assert cfg.force_reprocess_metadata is False

    def test_empty_value(self):
        cfg = Config.from_env(_env(FORCE_REPROCESS_METADATA=""))
        assert cfg.force_reprocess_metadata is False
