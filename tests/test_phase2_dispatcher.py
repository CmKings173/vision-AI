import time

from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import DeliveryStatus, TriggerEvent
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import FramePacket
from label_inspection.station.dispatcher import OutboxDispatcher
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.spool import LocalSpool, RecordType
from label_inspection.storage import InMemoryArtifactStore, StorageError
from tests.fixtures.quality import sharp_label


def _outcome(*, reject_quality: bool = False):
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=int(time.time() * 1000),
    )
    checker = QualityChecker(
        min_width=1,
        min_height=1,
        min_brightness=0,
        max_brightness=255,
        min_sharpness=1_000_000 if reject_quality else 0,
        max_underexposed_ratio=1,
        max_overexposed_ratio=1,
        max_glare_ratio=1,
    )
    packet = FramePacket(
        frame_id=11,
        captured_at=time.time() - 0.1,
        frame=sharp_label(),
        source="rtsp",
        captured_monotonic=time.monotonic() - 0.1,
    )
    preparer = StationPreparer(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        selector=FrameSelector(top_k=1, score_fn=lambda image: 1.0),
        quality_checker=checker,
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        bbox_padding_ratio=0.0,
    )
    return preparer.prepare_trigger([packet], trigger=trigger)


def test_dispatcher_uploads_exact_inference_record_before_artifacts_ready(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    original = spool.commit_outcome(_outcome(), provenance={"profile": "dgx-v1"})
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    dispatcher = OutboxDispatcher(spool=spool, store=store)

    report = dispatcher.dispatch_record(original)
    current = spool.open_record(original.state.event_id)

    assert report.uploaded_objects == 3
    assert report.artifact_upload_ms >= 0
    assert report.message_required is True
    assert current.state.delivery_status is DeliveryStatus.ARTIFACTS_READY
    assert current.frozen_job_bytes() == original.frozen_job_bytes()
    assert current.job is not None
    for reference in current.job.artifacts.values():
        assert store.get_verified(reference) == (
            current.path
            / ("selected_frame.jpg" if reference.key.endswith("selected_frame.jpg") else "label_crop.png")
        ).read_bytes()


def test_dispatcher_partial_storage_failure_keeps_local_only_for_safe_retry(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(_outcome(), provenance={"profile": "dgx-v1"})

    class FailingStore(InMemoryArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.ensure_bucket("vision-inspections")
            self.calls = 0

        def put_if_absent(self, reference, content):
            self.calls += 1
            if self.calls == 2:
                raise StorageError("simulated MinIO outage")
            return super().put_if_absent(reference, content)

    dispatcher = OutboxDispatcher(spool=spool, store=FailingStore())

    try:
        dispatcher.dispatch_record(record)
    except StorageError:
        pass
    else:
        raise AssertionError("storage failure must propagate to retry loop")

    assert spool.open_record(record.state.event_id).state.delivery_status is DeliveryStatus.LOCAL_ONLY


def test_terminal_record_uploads_result_without_creating_inference_message(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(_outcome(reject_quality=True))
    assert record.record_type is RecordType.TERMINAL_RESULT
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    dispatcher = OutboxDispatcher(spool=spool, store=store)

    report = dispatcher.dispatch_record(record)
    current = spool.open_record(record.state.event_id)

    assert report.message_required is False
    assert report.artifact_upload_ms >= 0
    assert report.uploaded_objects == 3
    assert (
        current.state.delivery_status
        is DeliveryStatus.TERMINAL_RESULT_DURABLE
    )
    assert current.result is not None


def test_terminal_record_resumes_from_artifacts_ready_after_restart(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(_outcome(reject_quality=True))
    spool.advance_delivery(record.state.event_id, DeliveryStatus.ARTIFACTS_READY)
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    dispatcher = OutboxDispatcher(spool=spool, store=store)

    reports = dispatcher.dispatch_pending()

    assert len(reports) == 1
    assert reports[0].message_required is False
    assert reports[0].uploaded_objects == 0
    assert (
        spool.open_record(record.state.event_id).state.delivery_status
        is DeliveryStatus.TERMINAL_RESULT_DURABLE
    )


def test_terminal_delivery_state_is_an_idempotent_dispatch_noop(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(_outcome(reject_quality=True))
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    dispatcher = OutboxDispatcher(spool=spool, store=store)
    dispatcher.dispatch_record(record)

    report = dispatcher.dispatch_record(spool.open_record(record.state.event_id))

    assert report.already_delivered is True
    assert report.uploaded_objects == 0
    assert report.delivery_status is DeliveryStatus.TERMINAL_RESULT_DURABLE


def test_dispatcher_recovery_is_idempotent_after_partial_upload(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(_outcome(), provenance={"profile": "dgx-v1"})
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    dispatcher = OutboxDispatcher(spool=spool, store=store)

    first = dispatcher.dispatch_record(record)
    second = dispatcher.dispatch_record(spool.open_record(record.state.event_id))

    assert first.uploaded_objects == 3
    assert second.uploaded_objects == 0
    assert second.already_delivered is True


def test_dispatch_batch_failure_does_not_starve_later_committed_records(tmp_path):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    blocked = spool.commit_outcome(_outcome(), provenance={"profile": "dgx-v1"})
    healthy = spool.commit_outcome(_outcome(), provenance={"profile": "dgx-v1"})

    class EventScopedFailureStore(InMemoryArtifactStore):
        def put_if_absent(self, reference, content):
            if blocked.state.event_id in reference.key:
                raise StorageError("simulated event-scoped storage failure")
            return super().put_if_absent(reference, content)

    store = EventScopedFailureStore()
    store.ensure_bucket("vision-inspections")
    dispatcher = OutboxDispatcher(spool=spool, store=store)

    reports = dispatcher.dispatch_pending()

    by_event = {report.event_id: report for report in reports}
    assert by_event[blocked.state.event_id].error_code == "ARTIFACT_STORAGE_ERROR"
    assert by_event[healthy.state.event_id].error_code is None
    assert spool.open_record(blocked.state.event_id).state.delivery_status is DeliveryStatus.LOCAL_ONLY
    assert spool.open_record(healthy.state.event_id).state.delivery_status is DeliveryStatus.ARTIFACTS_READY


def test_dispatcher_does_not_require_bucket_creation_permission_per_artifact(
    tmp_path
):
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(_outcome(), provenance={"profile": "dgx-v1"})

    class LeastPrivilegeStore(InMemoryArtifactStore):
        def ensure_bucket(self, bucket):
            raise AssertionError("dispatcher must not provision buckets")

    store = LeastPrivilegeStore()
    store._buckets.add("vision-inspections")

    report = OutboxDispatcher(spool=spool, store=store).dispatch_record(record)

    assert report.uploaded_objects == 3
