import hashlib

import pytest

from label_inspection.contracts import ArtifactRef, TriggerEvent
from label_inspection.storage import (
    ArtifactIntegrityError,
    DeferredArtifactStore,
    InMemoryArtifactStore,
    PutStatus,
    StorageConflictError,
    event_object_keys,
)


def _ref(*, key: str, content: bytes) -> ArtifactRef:
    return ArtifactRef(
        bucket="vision-inspections",
        key=key,
        sha256=hashlib.sha256(content).hexdigest(),
        content_type="application/octet-stream",
        size_bytes=len(content),
    )


def test_put_if_absent_is_checksum_idempotent_and_never_overwrites():
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    original = b"exact-crop"
    reference = _ref(key="station/event/source/label_crop.png", content=original)

    created = store.put_if_absent(reference, original)
    repeated = store.put_if_absent(reference, original)

    assert created.status is PutStatus.CREATED
    assert repeated.status is PutStatus.ALREADY_PRESENT
    assert store.get_verified(reference) == original

    conflicting = _ref(
        key="station/event/source/label_crop.png", content=b"different-crop"
    )
    with pytest.raises(StorageConflictError):
        store.put_if_absent(conflicting, b"different-crop")
    assert store.get_verified(reference) == original


def test_content_is_verified_against_reference_before_storage_io():
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    reference = _ref(key="station/event/metadata/job.json", content=b"expected")

    with pytest.raises(ArtifactIntegrityError, match="checksum") as raised:
        store.put_if_absent(reference, b"tampered")

    error = raised.value.to_inspection_error()
    assert error.code == "ARTIFACT_CHECKSUM_MISMATCH"
    assert error.stage == "ARTIFACT_STORAGE"
    assert error.retryable is False
    assert store.head(reference.bucket, reference.key) is None


def test_deferred_store_retries_connection_after_transient_failure():
    backend = InMemoryArtifactStore()
    backend.ensure_bucket("vision-inspections")
    attempts = 0

    def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("MinIO unavailable")
        return backend

    store = DeferredArtifactStore(
        bucket="vision-inspections",
        store_factory=connect,
    )
    content = b"recoverable-artifact"
    reference = _ref(key="station/event/source/label_crop.png", content=content)

    with pytest.raises(RuntimeError, match="unavailable"):
        store.put_if_absent(reference, content)

    result = store.put_if_absent(reference, content)

    assert result.status is PutStatus.CREATED
    assert attempts == 2


def test_event_object_keys_are_deterministic_and_separate_domains():
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=1_787_623_900_000,
    )

    keys = event_object_keys(
        station_id=trigger.station_id,
        event_id=trigger.event_id,
        occurred_at_ms=trigger.triggered_at_ms,
    )

    assert keys.selected_frame.endswith("/source/selected_frame.jpg")
    assert keys.label_crop.endswith("/source/label_crop.png")
    assert keys.job.endswith("/metadata/job.json")
    assert keys.result.endswith("/result/result.json")
    assert trigger.event_id in keys.prefix
    assert keys == event_object_keys(
        station_id=trigger.station_id,
        event_id=trigger.event_id,
        occurred_at_ms=trigger.triggered_at_ms,
    )


@pytest.mark.parametrize("station_id", ["../station", "station/path", "station\\path"])
def test_event_object_keys_reject_unsafe_station_segments(station_id):
    event_id = TriggerEvent.create(
        station_id="STATION-01", camera_id="PHONE-01"
    ).event_id

    with pytest.raises(ValueError, match="station_id"):
        event_object_keys(
            station_id=station_id,
            event_id=event_id,
            occurred_at_ms=1,
        )
