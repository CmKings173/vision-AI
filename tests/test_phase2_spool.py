import hashlib
import json
import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import DeliveryStatus, TriggerEvent
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import FramePacket
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.spool import (
    FileManifest,
    LocalSpool,
    RecordType,
    SpoolCapacityError,
    SpoolCommitError,
    SpoolConflictError,
    SpoolLimits,
    SpoolPathError,
    SpoolState,
    SpoolStateError,
)
from tests.fixtures.quality import sharp_label

pytestmark = pytest.mark.integration


def _quality_pass() -> QualityChecker:
    return QualityChecker(
        min_width=1,
        min_height=1,
        min_brightness=0,
        max_brightness=255,
        min_sharpness=0,
        max_underexposed_ratio=1,
        max_overexposed_ratio=1,
        max_glare_ratio=1,
    )


def _outcome(*, quality_checker=None):
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=int(time.time() * 1000),
    )
    frame = sharp_label()
    packet = FramePacket(
        frame_id=7,
        captured_at=time.time() - 0.1,
        frame=frame,
        source="rtsp",
        captured_monotonic=time.monotonic() - 0.1,
    )
    preparer = StationPreparer(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        selector=FrameSelector(top_k=1, score_fn=lambda image: 1.0),
        quality_checker=quality_checker or _quality_pass(),
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        bbox_padding_ratio=0.0,
    )
    return preparer.prepare_trigger([packet], trigger=trigger)


def test_atomic_inference_commit_persists_exact_crop_and_frozen_job(tmp_path):
    outcome = _outcome()
    assert outcome.prepared is not None
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")

    record = spool.commit_outcome(
        outcome,
        provenance={
            "profile": "dgx_spark_label",
            "extractor_version": "v1",
            "locator_version": "fixed-roi.v1",
        },
    )

    assert record.record_type is RecordType.INFERENCE_JOB
    assert record.state.delivery_status is DeliveryStatus.LOCAL_ONLY
    assert record.path == (tmp_path / "spool" / outcome.event_id).resolve()
    assert record.path.is_dir()
    assert not (tmp_path / "spool" / f".tmp_{outcome.event_id}").exists()
    assert {path.name for path in record.path.iterdir()} == {
        "selected_frame.jpg",
        "label_crop.png",
        "job.json",
        "state.json",
    }

    crop_bytes = (record.path / "label_crop.png").read_bytes()
    decoded = cv2.imdecode(np.frombuffer(crop_bytes, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(decoded, outcome.prepared.label_crop)

    assert record.job is not None
    crop_ref = record.job.artifacts["label_crop"]
    assert crop_ref.sha256 == hashlib.sha256(crop_bytes).hexdigest()
    assert crop_ref.size_bytes == len(crop_bytes)
    assert crop_ref.content_type == "image/png"
    assert outcome.event_id in crop_ref.key
    assert crop_ref.key.endswith("/source/label_crop.png")
    assert record.job.locator["orientation_degrees"] == 0
    assert record.job.to_dict() == json.loads(record.frozen_job_bytes())
    assert record.state.files["label_crop.png"].sha256 == crop_ref.sha256


def test_committed_job_cannot_be_rebuilt_or_overwritten(tmp_path):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")
    record = spool.commit_outcome(outcome, provenance={"profile": "v1"})
    frozen_before = record.frozen_job_bytes()

    with pytest.raises(SpoolConflictError):
        spool.commit_outcome(outcome, provenance={"profile": "changed"})

    assert record.frozen_job_bytes() == frozen_before


def test_failed_atomic_rename_never_exposes_dispatchable_final_event(
    tmp_path, monkeypatch
):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")

    def fail_rename(source, destination):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("label_inspection.station.spool.os.replace", fail_rename)

    with pytest.raises(SpoolCommitError, match="atomic commit"):
        spool.commit_outcome(outcome, provenance={"profile": "v1"})

    assert not (tmp_path / "spool" / outcome.event_id).exists()
    assert (tmp_path / "spool" / f".tmp_{outcome.event_id}").is_dir()


def test_required_file_fsync_failure_rejects_local_commit(tmp_path, monkeypatch):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")

    def fail_fsync(_descriptor):
        raise OSError("simulated file fsync failure")

    monkeypatch.setattr("label_inspection.station.spool.os.fsync", fail_fsync)

    with pytest.raises(SpoolCommitError, match="commit failed"):
        spool.commit_outcome(outcome, provenance={"profile": "v1"})

    assert not spool.event_path(outcome.event_id).exists()


def test_required_parent_directory_open_failure_rejects_local_commit(
    tmp_path, monkeypatch
):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")
    original_open = os.open

    def fail_parent_open(path, flags):
        if Path(path) == spool.root:
            raise OSError("simulated parent directory open failure")
        return original_open(path, flags)

    monkeypatch.setattr(
        "label_inspection.station.spool._DIRECTORY_FSYNC_REQUIRED", True, raising=False
    )
    monkeypatch.setattr("label_inspection.station.spool.os.open", fail_parent_open)

    with pytest.raises(SpoolCommitError, match="directory durability"):
        spool.commit_outcome(outcome, provenance={"profile": "v1"})


def test_required_parent_directory_fsync_failure_rejects_local_commit(
    tmp_path, monkeypatch
):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")
    original_open = os.open
    original_fsync = os.fsync
    parent_descriptor: int | None = None

    def capture_parent_open(path, flags):
        nonlocal parent_descriptor
        descriptor = original_open(path, flags)
        if Path(path) == spool.root:
            parent_descriptor = descriptor
        return descriptor

    def fail_parent_fsync(descriptor):
        if descriptor == parent_descriptor:
            raise OSError("simulated parent directory fsync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(
        "label_inspection.station.spool._DIRECTORY_FSYNC_REQUIRED", True, raising=False
    )
    monkeypatch.setattr("label_inspection.station.spool.os.open", capture_parent_open)
    monkeypatch.setattr("label_inspection.station.spool.os.fsync", fail_parent_fsync)

    with pytest.raises(SpoolCommitError, match="directory durability"):
        spool.commit_outcome(outcome, provenance={"profile": "v1"})


def test_successful_commit_completes_durability_sequence_before_return(
    tmp_path, monkeypatch
):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")
    observed: list[str] = []
    original_write = spool._write_bytes
    original_directory_fsync = spool._fsync_directory
    original_replace = os.replace

    def track_write(path, content):
        original_write(path, content)
        observed.append(f"file:{Path(path).name}")

    def track_directory_fsync(path):
        original_directory_fsync(path)
        observed.append(f"directory:{Path(path).name}")

    def track_replace(source, destination):
        observed.append("rename")
        return original_replace(source, destination)

    monkeypatch.setattr(spool, "_write_bytes", track_write)
    monkeypatch.setattr(spool, "_fsync_directory", track_directory_fsync)
    monkeypatch.setattr("label_inspection.station.spool.os.replace", track_replace)

    record = spool.commit_outcome(outcome, provenance={"profile": "v1"})

    temp_sync = observed.index(f"directory:.tmp_{outcome.event_id}")
    rename = observed.index("rename")
    parent_sync = observed.index(f"directory:{spool.root.name}")
    assert all(item.startswith("file:") for item in observed[:temp_sync])
    assert temp_sync < rename < parent_sync
    assert record.path.is_dir()


@pytest.mark.parametrize(
    "unsafe_id",
    ["../event", "event/path", "event\\path", "C:\\event", "INS-001"],
)
def test_event_path_rejects_traversal_separators_drive_and_non_uuid(
    tmp_path, unsafe_id
):
    spool = LocalSpool(tmp_path / "spool")

    with pytest.raises(SpoolPathError):
        spool.event_path(unsafe_id)


def test_existing_symlink_cannot_escape_spool_root(tmp_path):
    spool_root = tmp_path / "spool"
    outside = tmp_path / "outside"
    outside.mkdir()
    spool = LocalSpool(spool_root)
    event_id = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
    ).event_id
    event_link = spool_root / event_id
    try:
        event_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(SpoolPathError, match="outside spool root"):
        spool.event_path(event_id)


@pytest.mark.parametrize("unsafe_name", ["../outside", "..\\outside", "dir/file"])
def test_state_manifest_rejects_path_separators_on_every_platform(unsafe_name):
    event_id = TriggerEvent.create(
        station_id="STATION-01", camera_id="PHONE-01"
    ).event_id
    manifest = FileManifest.from_bytes(b"evidence", content_type="image/png")

    with pytest.raises(ValueError, match="must not contain a path"):
        SpoolState(
            event_id=event_id,
            record_type=RecordType.INFERENCE_JOB,
            delivery_status=DeliveryStatus.LOCAL_ONLY,
            created_at_ms=1,
            updated_at_ms=1,
            files={unsafe_name: manifest},
        )


def test_spool_commit_error_exposes_safe_structured_error(tmp_path, monkeypatch):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")

    def fail_rename(source, destination):
        raise OSError("D:/secret/internal/path")

    monkeypatch.setattr("label_inspection.station.spool.os.replace", fail_rename)

    with pytest.raises(SpoolCommitError) as raised:
        spool.commit_outcome(outcome, provenance={"profile": "v1"})

    error = raised.value.to_inspection_error()
    assert error.code == "SPOOL_COMMIT_ERROR"
    assert error.stage == "LOCAL_SPOOL"
    assert "secret" not in error.message.lower()
    assert error.retryable is True


def test_quality_rejection_commits_terminal_result_with_available_artifacts(tmp_path):
    rejecting_checker = QualityChecker(
        min_width=1,
        min_height=1,
        min_brightness=0,
        max_brightness=255,
        min_sharpness=1_000_000,
        max_underexposed_ratio=1,
        max_overexposed_ratio=1,
        max_glare_ratio=1,
    )
    outcome = _outcome(quality_checker=rejecting_checker)
    assert outcome.inference_required is False
    assert outcome.prepared is not None
    spool = LocalSpool(tmp_path / "spool")

    record = spool.commit_outcome(outcome, provenance={"station_profile": "dgx-v1"})

    assert record.record_type is RecordType.TERMINAL_RESULT
    assert record.job is None
    assert record.result is not None
    assert record.result.processing_status.value == "COMPLETED"
    assert record.result.business_status.value == "REVIEW"
    assert record.result.inference_executed is False
    assert "QUALITY_REJECTED" in record.result.reasons
    assert {path.name for path in record.path.iterdir()} == {
        "selected_frame.jpg",
        "label_crop.png",
        "result.json",
        "state.json",
    }
    assert json.loads((record.path / "result.json").read_bytes()) == record.result.to_dict()
    assert record.result.result_payload["provenance"]["station_profile"] == "dgx-v1"


def test_early_preparation_error_commits_terminal_result_without_image_artifacts(
    tmp_path,
):
    trigger = TriggerEvent.create(station_id="STATION-01", camera_id="PHONE-01")
    preparer = StationPreparer(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
    )
    outcome = preparer.prepare_trigger([], trigger=trigger)
    spool = LocalSpool(tmp_path / "spool")

    record = spool.commit_outcome(outcome)

    assert record.record_type is RecordType.TERMINAL_RESULT
    assert record.result is not None
    assert record.result.processing_status.value == "ERROR"
    assert record.result.business_status is None
    assert record.result.error is not None
    assert record.result.error.code == "NO_FRAME_CANDIDATE"
    assert {path.name for path in record.path.iterdir()} == {
        "result.json",
        "state.json",
    }


def test_delivery_state_moves_forward_atomically_without_mutating_job(tmp_path):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")
    record = spool.commit_outcome(outcome, provenance={"profile": "v1"})
    frozen_job = record.frozen_job_bytes()

    with pytest.raises(SpoolStateError):
        spool.advance_delivery(outcome.event_id, DeliveryStatus.JOB_PUBLISHED)

    artifacts_ready = spool.advance_delivery(
        outcome.event_id, DeliveryStatus.ARTIFACTS_READY
    )
    assert artifacts_ready.state.delivery_status is DeliveryStatus.ARTIFACTS_READY
    assert artifacts_ready.frozen_job_bytes() == frozen_job

    published = spool.advance_delivery(
        outcome.event_id, DeliveryStatus.JOB_PUBLISHED
    )
    assert published.state.delivery_status is DeliveryStatus.JOB_PUBLISHED
    assert published.frozen_job_bytes() == frozen_job

    idempotent = spool.advance_delivery(
        outcome.event_id, DeliveryStatus.JOB_PUBLISHED
    )
    assert idempotent.state.delivery_status is DeliveryStatus.JOB_PUBLISHED
    with pytest.raises(SpoolStateError):
        spool.advance_delivery(outcome.event_id, DeliveryStatus.LOCAL_ONLY)


def test_terminal_delivery_uses_distinct_terminal_state(tmp_path):
    rejecting_checker = QualityChecker(
        min_width=1,
        min_height=1,
        min_brightness=0,
        max_brightness=255,
        min_sharpness=1_000_000,
        max_underexposed_ratio=1,
        max_overexposed_ratio=1,
        max_glare_ratio=1,
    )
    outcome = _outcome(quality_checker=rejecting_checker)
    spool = LocalSpool(tmp_path / "spool")
    spool.commit_outcome(outcome)
    spool.advance_delivery(outcome.event_id, DeliveryStatus.ARTIFACTS_READY)

    terminal = spool.advance_delivery(
        outcome.event_id, DeliveryStatus.TERMINAL_RESULT_DURABLE
    )

    assert terminal.record_type is RecordType.TERMINAL_RESULT
    assert (
        terminal.state.delivery_status
        is DeliveryStatus.TERMINAL_RESULT_DURABLE
    )
    with pytest.raises(SpoolStateError):
        spool.advance_delivery(outcome.event_id, DeliveryStatus.JOB_PUBLISHED)


@pytest.mark.parametrize(
    ("record_type", "delivery_status"),
    [
        (RecordType.INFERENCE_JOB, DeliveryStatus.TERMINAL_RESULT_DURABLE),
        (RecordType.TERMINAL_RESULT, DeliveryStatus.JOB_PUBLISHED),
    ],
)
def test_spool_state_rejects_terminal_status_for_wrong_record_kind(
    record_type, delivery_status
):
    event_id = TriggerEvent.create(
        station_id="STATION-01", camera_id="PHONE-01"
    ).event_id

    with pytest.raises(ValueError, match="incompatible"):
        SpoolState(
            event_id=event_id,
            record_type=record_type,
            delivery_status=delivery_status,
            created_at_ms=1,
            updated_at_ms=1,
            files={},
        )


def test_failed_state_replace_preserves_previous_committed_state(tmp_path, monkeypatch):
    outcome = _outcome()
    spool = LocalSpool(tmp_path / "spool")
    record = spool.commit_outcome(outcome, provenance={"profile": "v1"})
    old_state = (record.path / "state.json").read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated state replace failure")

    monkeypatch.setattr("label_inspection.station.spool.os.replace", fail_replace)

    with pytest.raises(SpoolStateError, match="atomic state update"):
        spool.advance_delivery(outcome.event_id, DeliveryStatus.ARTIFACTS_READY)

    assert (record.path / "state.json").read_bytes() == old_state
    assert (record.path / ".state.json.tmp").is_file()


def test_recovery_scan_validates_checksums_and_preserves_incomplete_evidence(tmp_path):
    spool = LocalSpool(tmp_path / "spool")
    valid_outcome = _outcome()
    corrupt_outcome = _outcome()
    valid = spool.commit_outcome(valid_outcome, provenance={"profile": "v1"})
    corrupt = spool.commit_outcome(corrupt_outcome, provenance={"profile": "v1"})
    (corrupt.path / "label_crop.png").write_bytes(b"corrupt")

    incomplete_event = TriggerEvent.create(
        station_id="STATION-01", camera_id="PHONE-01"
    ).event_id
    incomplete = spool.root / f".tmp_{incomplete_event}"
    incomplete.mkdir()
    (incomplete / "partial.bin").write_bytes(b"partial")

    report = spool.scan_recovery()

    assert [item.state.event_id for item in report.pending_records] == [
        valid_outcome.event_id
    ]
    assert {issue.event_id for issue in report.corrupt_records} == {
        corrupt_outcome.event_id
    }
    assert report.corrupt_records[0].code == "CHECKSUM_MISMATCH"
    assert report.incomplete_paths == (incomplete.resolve(),)
    assert valid.path.exists()
    assert corrupt.path.exists()
    assert incomplete.exists()


def test_event_limit_rejects_second_commit_before_temp_or_final_is_created(tmp_path):
    spool = LocalSpool(
        tmp_path / "spool",
        limits=SpoolLimits(
            max_pending_events=1,
            max_pending_bytes=100_000_000,
            min_free_disk_bytes=0,
        ),
    )
    first = _outcome()
    second = _outcome()
    spool.commit_outcome(first, provenance={"profile": "v1"})

    with pytest.raises(SpoolCapacityError) as raised:
        spool.commit_outcome(second, provenance={"profile": "v1"})

    assert raised.value.reason == "SPOOL_MAX_PENDING_EVENTS"
    assert raised.value.to_inspection_error().code == "SPOOL_MAX_PENDING_EVENTS"
    assert not (spool.root / second.event_id).exists()
    assert not (spool.root / f".tmp_{second.event_id}").exists()


def test_exact_projected_bytes_are_checked_before_commit(tmp_path):
    spool = LocalSpool(
        tmp_path / "spool",
        limits=SpoolLimits(
            max_pending_events=10,
            max_pending_bytes=128,
            min_free_disk_bytes=0,
        ),
    )
    outcome = _outcome()

    with pytest.raises(SpoolCapacityError) as raised:
        spool.commit_outcome(outcome, provenance={"profile": "v1"})

    assert raised.value.reason == "SPOOL_MAX_PENDING_BYTES"
    assert not (spool.root / outcome.event_id).exists()
    assert not (spool.root / f".tmp_{outcome.event_id}").exists()


def test_incomplete_and_corrupt_entries_count_conservatively_as_pending(tmp_path):
    spool = LocalSpool(
        tmp_path / "spool",
        limits=SpoolLimits(
            max_pending_events=2,
            max_pending_bytes=100_000_000,
            min_free_disk_bytes=0,
        ),
    )
    incomplete = spool.root / f".tmp_{_outcome().event_id}"
    incomplete.mkdir()
    (incomplete / "partial.bin").write_bytes(b"partial")
    corrupt = spool.root / _outcome().event_id
    corrupt.mkdir()
    (corrupt / "state.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(SpoolCapacityError) as raised:
        spool.check_capacity()

    assert raised.value.reason == "SPOOL_MAX_PENDING_EVENTS"
    assert raised.value.usage.pending_events == 2
    assert raised.value.usage.pending_bytes >= len(b"partial")


def test_delivered_record_no_longer_consumes_pending_event_capacity(tmp_path):
    spool = LocalSpool(
        tmp_path / "spool",
        limits=SpoolLimits(
            max_pending_events=1,
            max_pending_bytes=100_000_000,
            min_free_disk_bytes=0,
        ),
    )
    first = _outcome()
    spool.commit_outcome(first, provenance={"profile": "v1"})
    spool.advance_delivery(first.event_id, DeliveryStatus.ARTIFACTS_READY)
    spool.advance_delivery(first.event_id, DeliveryStatus.JOB_PUBLISHED)

    usage = spool.check_capacity()

    assert usage.pending_events == 0
    second = _outcome()
    assert spool.commit_outcome(second, provenance={"profile": "v1"}).job is not None


def test_disk_space_probe_failure_fails_closed_with_structured_technical_reason(
    tmp_path, monkeypatch
):
    spool = LocalSpool(
        tmp_path / "spool",
        limits=SpoolLimits(
            max_pending_events=10,
            max_pending_bytes=100_000_000,
            min_free_disk_bytes=1,
        ),
    )

    def fail_disk_probe(path):
        raise OSError("D:/secret/disk")

    monkeypatch.setattr("label_inspection.station.spool.shutil.disk_usage", fail_disk_probe)

    with pytest.raises(SpoolCapacityError) as raised:
        spool.check_capacity()

    error = raised.value.to_inspection_error()
    assert raised.value.reason == "SPOOL_DISK_PROBE_ERROR"
    assert error.code == "SPOOL_DISK_PROBE_ERROR"
    assert error.stage == "LOCAL_SPOOL"
    assert error.retryable is True
    assert "secret" not in error.message.lower()
