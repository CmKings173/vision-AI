import json
import math
import uuid

import pytest

from label_inspection.contracts import (
    JOB_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    ArtifactRef,
    BusinessStatus,
    ContractValidationError,
    DeliveryStatus,
    InspectionError,
    InspectionJob,
    InspectionResult,
    ProcessingStatus,
    TriggerEvent,
)


def _artifact() -> ArtifactRef:
    return ArtifactRef(
        bucket="vision-inspections",
        key="STATION-01/2026/08/25/event/source/label_crop.png",
        sha256="a" * 64,
        content_type="image/png",
        size_bytes=1234,
    )


def _job() -> InspectionJob:
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_777_000_000_000,
    )
    return InspectionJob(
        event_id=trigger.event_id,
        trigger_id=trigger.trigger_id,
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        triggered_at_ms=trigger.triggered_at_ms,
        received_at_ms=1_777_000_000_010,
        source_timestamp_ms=None,
        prepared_at_ms=1_777_000_000_020,
        created_at_ms=1_777_000_000_021,
        artifacts={"label_crop": _artifact()},
        selection={"frame_id": 7},
        locator={"name": "fixed_roi", "version": "v1"},
        quality={"status": "PASS"},
        provenance={"profile": "dgx_spark_label", "extractor_version": "v1"},
    )


def test_trigger_event_generates_valid_distinct_uuid4_identity():
    first = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_777_000_000_000,
    )
    second = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_777_000_000_001,
    )

    assert uuid.UUID(first.event_id).version == 4
    assert uuid.UUID(first.trigger_id).version == 4
    assert first.event_id != first.trigger_id
    assert first.event_id != second.event_id


@pytest.mark.parametrize(
    "invalid_id",
    ["INS-001", "../event", "event/path", "event\\path", "C:\\event"],
)
def test_distributed_contract_rejects_non_uuid_identity(invalid_id):
    payload = _job().to_dict()
    payload["event_id"] = invalid_id

    with pytest.raises(ContractValidationError, match="event_id"):
        InspectionJob.from_dict(payload)


@pytest.mark.parametrize("invalid_time", [1.5, -1, "1777000000000", True])
def test_distributed_contract_requires_non_negative_integer_epoch_ms(invalid_time):
    payload = _job().to_dict()
    payload["received_at_ms"] = invalid_time

    with pytest.raises(ContractValidationError, match="received_at_ms"):
        InspectionJob.from_dict(payload)


def test_rtsp_job_uses_received_time_without_inventing_capture_timestamp():
    payload = _job().to_dict()

    assert payload["received_at_ms"] == 1_777_000_000_010
    assert payload["source_timestamp_ms"] is None
    assert "captured_at_ms" not in payload


def test_buffered_frame_may_be_received_before_manual_trigger():
    payload = _job().to_dict()
    payload["received_at_ms"] = payload["triggered_at_ms"] - 250

    restored = InspectionJob.from_dict(payload)

    assert restored.received_at_ms < restored.triggered_at_ms
    assert restored.prepared_at_ms >= restored.triggered_at_ms


def test_artifact_ref_contains_only_reference_and_integrity_metadata():
    artifact = _artifact()
    payload = artifact.to_dict()

    assert payload == {
        "bucket": "vision-inspections",
        "key": "STATION-01/2026/08/25/event/source/label_crop.png",
        "sha256": "a" * 64,
        "content_type": "image/png",
        "size_bytes": 1234,
    }
    assert "bytes" not in payload
    assert "data" not in payload


def test_job_round_trip_is_versioned_json_and_rejects_unknown_fields():
    job = _job()
    payload = job.to_dict()

    assert payload["schema_version"] == JOB_SCHEMA_VERSION
    assert payload["processing_status"] == "PREPARED"
    assert InspectionJob.from_dict(json.loads(json.dumps(payload))) == job

    payload["image_bytes"] = "not-allowed"
    with pytest.raises(ContractValidationError, match="unknown fields"):
        InspectionJob.from_dict(payload)


def test_job_rejects_unsupported_contract_version():
    payload = _job().to_dict()
    payload["schema_version"] = "inspection-job.v2"

    with pytest.raises(ContractValidationError, match="schema_version"):
        InspectionJob.from_dict(payload)


def test_job_copies_and_freezes_nested_metadata_at_construction():
    provenance = {"profile": "dgx_spark_label", "versions": ["extractor-v1"]}
    job = _job()
    job = InspectionJob.from_dict(
        {**job.to_dict(), "provenance": provenance}
    )

    provenance["profile"] = "mutated"
    provenance["versions"].append("mutated")

    assert job.to_dict()["provenance"] == {
        "profile": "dgx_spark_label",
        "versions": ["extractor-v1"],
    }
    with pytest.raises(TypeError):
        job.provenance["profile"] = "cannot-change"


@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
def test_contract_metadata_rejects_non_finite_json_numbers(non_finite):
    payload = _job().to_dict()
    payload["quality"]["sharpness"] = non_finite

    with pytest.raises(ContractValidationError, match="finite"):
        InspectionJob.from_dict(payload)


def test_status_domains_are_distinct_and_have_explicit_owners():
    processing = {item.value for item in ProcessingStatus}
    business = {item.value for item in BusinessStatus}
    delivery = {item.value for item in DeliveryStatus}

    assert processing == {
        "CREATED",
        "CAPTURED",
        "PREPARED",
        "QUEUED",
        "PROCESSING",
        "COMPLETED",
        "ERROR",
    }
    assert business == {"PASS", "REVIEW", "FAIL"}
    assert delivery == {
        "LOCAL_ONLY",
        "ARTIFACTS_READY",
        "JOB_PUBLISHED",
        "TERMINAL_RESULT_DURABLE",
    }
    assert business.isdisjoint(delivery)


def test_quality_rejected_is_terminal_review_without_inference_job():
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_777_000_000_000,
    )
    result = InspectionResult.quality_rejected(
        event_id=trigger.event_id,
        trigger_id=trigger.trigger_id,
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        created_at_ms=1_777_000_000_020,
        completed_at_ms=1_777_000_000_021,
        quality={"status": "FAIL", "reasons": ["GLARE"]},
        reasons=("QUALITY_REJECTED", "QUALITY_GLARE"),
    )
    payload = result.to_dict()

    assert payload["schema_version"] == RESULT_SCHEMA_VERSION
    assert payload["processing_status"] == "COMPLETED"
    assert payload["business_status"] == "REVIEW"
    assert payload["inference_executed"] is False
    assert "QUALITY_REJECTED" in payload["reasons"]
    assert payload["error"] is None


def test_preparation_error_is_technical_terminal_result_not_business_fail():
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_777_000_000_000,
    )
    error = InspectionError(
        code="CAMERA_STALE_FRAME",
        stage="PREPARATION",
        message="No fresh frame is available.",
        retryable=True,
        attempt=0,
    )
    result = InspectionResult.preparation_error(
        event_id=trigger.event_id,
        trigger_id=trigger.trigger_id,
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        created_at_ms=1_777_000_000_010,
        completed_at_ms=1_777_000_000_011,
        error=error,
    )
    payload = result.to_dict()

    assert payload["processing_status"] == "ERROR"
    assert payload["business_status"] is None
    assert payload["inference_executed"] is False
    assert payload["error"]["code"] == "CAMERA_STALE_FRAME"


def test_result_rejects_unsupported_version_and_invalid_status_combination():
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_777_000_000_000,
    )
    result = InspectionResult.quality_rejected(
        event_id=trigger.event_id,
        trigger_id=trigger.trigger_id,
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        created_at_ms=1_777_000_000_020,
        completed_at_ms=1_777_000_000_021,
        quality={"status": "FAIL"},
    )
    payload = result.to_dict()
    payload["schema_version"] = "inspection-result.v2"
    with pytest.raises(ContractValidationError, match="schema_version"):
        InspectionResult.from_dict(payload)

    payload = result.to_dict()
    payload["processing_status"] = "ERROR"
    payload["business_status"] = "FAIL"
    with pytest.raises(ContractValidationError):
        InspectionResult.from_dict(payload)

    payload = result.to_dict()
    payload["processing_status"] = "PROCESSING"
    payload["business_status"] = None
    with pytest.raises(ContractValidationError, match="terminal"):
        InspectionResult.from_dict(payload)

    payload = result.to_dict()
    payload["business_status"] = None
    with pytest.raises(ContractValidationError, match="business_status"):
        InspectionResult.from_dict(payload)
