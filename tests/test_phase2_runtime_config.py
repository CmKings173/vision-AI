from dataclasses import replace

import pytest

from label_inspection.app import build_local_spool
from label_inspection.config import ConfigError, Settings


def _configured(**overrides):
    config = Settings(
        detector="fixed-roi",
        label_roi="0.1,0.1,0.9,0.9",
        minio_endpoint="gx10:9000",
        minio_access_key="vision-station",
        minio_secret_key="top-secret",
        rabbitmq_url="amqp://vision:password@gx10:5672/%2F",
        ocr_engine="ppocr_v6",
        ocr_backend="transformers",
        ocr_version="PP-OCRv6",
    )
    return replace(config, **overrides)


def test_phase2_transport_config_validates_and_hides_credentials_from_repr(tmp_path):
    config = _configured(spool_root=str(tmp_path / "spool"))

    config.validate_phase2_station()

    rendered = repr(config)
    assert "top-secret" not in rendered
    assert "amqp://vision:password" not in rendered
    assert "vision-station" not in rendered


@pytest.mark.parametrize(
    "override",
    [
        {"minio_endpoint": None},
        {"minio_access_key": None},
        {"minio_secret_key": None},
        {"rabbitmq_url": None},
    ],
)
def test_phase2_station_fails_fast_when_transport_config_is_missing(override):
    with pytest.raises(ValueError):
        _configured(**override).validate_phase2_station()


def test_local_spool_uses_configured_artifact_bucket(tmp_path):
    config = _configured(
        spool_root=str(tmp_path / "spool"), artifact_bucket="custom-inspections"
    )

    spool = build_local_spool(config)

    assert spool.bucket == "custom-inspections"


def test_phase2_worker_requires_resident_ppocr_v6_lifecycle():
    with pytest.raises(ValueError, match="PP-OCRv6"):
        _configured(ocr_engine="ppocr").validate_phase2_worker()


def test_retry_schedule_is_environment_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("VISION_RETRY_DELAYS_MS", "1000,5000,15000")

    config = Settings()

    assert config.retry_delays_ms == (1000, 5000, 15000)
    _configured(retry_delays_ms=(1000, 5000, 15000)).validate_phase2_transport()
    with pytest.raises(ValueError, match="RETRY_DELAYS"):
        _configured(retry_delays_ms=(5000, 1000)).validate_phase2_transport()


def test_station_id_must_be_safe_for_deterministic_object_keys():
    with pytest.raises(ValueError, match="VISION_STATION_ID"):
        _configured(station_id="../unsafe").validate_phase2_station()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
    ],
)
def test_security_boolean_environment_uses_explicit_grammar(
    monkeypatch, raw, expected
):
    monkeypatch.setenv("VISION_MINIO_SECURE", raw)

    assert Settings().minio_secure is expected


@pytest.mark.parametrize("raw", ["tru", "truee", "ture", "on", "off", "2", ""])
def test_invalid_security_boolean_fails_closed_at_settings_load(monkeypatch, raw):
    monkeypatch.setenv("VISION_MINIO_SECURE", raw)

    with pytest.raises(ConfigError, match="VISION_MINIO_SECURE"):
        Settings()


@pytest.mark.parametrize(
    "raw",
    ["NaN", "Infinity", "-Infinity", "-1", "0", "60.0001"],
)
def test_dispatch_interval_rejects_nonfinite_nonpositive_and_excessive_values(
    monkeypatch, raw
):
    monkeypatch.setenv("VISION_DISPATCH_INTERVAL_S", raw)

    with pytest.raises(ConfigError, match="VISION_DISPATCH_INTERVAL_S"):
        Settings()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_direct_nonfinite_safety_config_fails_validation(value):
    with pytest.raises(ConfigError):
        _configured(
            dispatch_interval_s=value,
            quality_max_glare_ratio=value,
            ocr_confidence=value,
        ).validate_phase2_worker()


def test_phase2_resource_limits_are_configurable_and_positive(monkeypatch):
    monkeypatch.setenv("VISION_MAX_JOB_MESSAGE_BYTES", "1048576")
    monkeypatch.setenv("VISION_MAX_LABEL_CROP_BYTES", "16777216")
    monkeypatch.setenv("VISION_MAX_IMAGE_PIXELS", "16000000")

    config = Settings()

    assert config.max_job_message_bytes == 1_048_576
    assert config.max_label_crop_bytes == 16_777_216
    assert config.max_image_pixels == 16_000_000
    _configured(
        max_job_message_bytes=1_048_576,
        max_label_crop_bytes=16_777_216,
        max_image_pixels=16_000_000,
    ).validate_phase2_worker()
    with pytest.raises(ConfigError):
        _configured(max_job_message_bytes=0).validate_phase2_worker()
