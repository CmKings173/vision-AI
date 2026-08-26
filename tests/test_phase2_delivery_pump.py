import time

from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import DeliveryStatus, TriggerEvent
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.messaging import FrozenJobPublisher, StructuredLifecycleLogger
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import FramePacket
from label_inspection.station.dispatcher import OutboxDispatcher
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.service import DeliveryPump
from label_inspection.station.spool import LocalSpool
from label_inspection.storage import InMemoryArtifactStore
from tests.fixtures.quality import sharp_label


class _Confirmed:
    def __init__(self) -> None:
        self.bodies = []

    def publish(self, **kwargs):
        self.bodies.append(kwargs["body"])


def _store():
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    return store


def _record(tmp_path, *, reject_quality: bool = False):
    trigger = TriggerEvent.create(
        station_id="STATION-01", camera_id="PHONE-01"
    )
    packet = FramePacket(
        frame_id=31,
        captured_at=time.time() - 0.01,
        frame=sharp_label(),
        captured_monotonic=time.monotonic(),
    )
    preparer = StationPreparer(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        selector=FrameSelector(top_k=1, score_fn=lambda image: 1.0),
        quality_checker=QualityChecker(
            min_width=1,
            min_height=1,
            min_brightness=0,
            max_brightness=255,
            min_sharpness=1_000_000 if reject_quality else 0,
            max_underexposed_ratio=1,
            max_overexposed_ratio=1,
            max_glare_ratio=1,
        ),
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        bbox_padding_ratio=0,
    )
    spool = LocalSpool(tmp_path / "spool")
    outcome = preparer.prepare_trigger([packet], trigger=trigger)
    return spool, spool.commit_outcome(outcome)


def test_delivery_pump_orders_minio_before_confirmed_rabbit_publish(tmp_path):
    spool, record = _record(tmp_path)
    transport = _Confirmed()
    job_publisher = FrozenJobPublisher(spool=spool, publisher=transport)
    lines = []
    pump = DeliveryPump(
        dispatcher=OutboxDispatcher(spool=spool, store=_store()),
        publisher_factory=lambda: job_publisher,
        lifecycle_logger=StructuredLifecycleLogger(sink=lines.append),
    )

    report = pump.run_once()

    assert report.uploaded_records == 1
    assert report.published_jobs == 1
    assert transport.bodies == [record.frozen_job_bytes()]
    assert any(record.state.event_id in line and "ARTIFACT_DISPATCH" in line for line in lines)
    assert any(record.state.event_id in line and "JOB_PUBLISH" in line for line in lines)
    assert (
        spool.open_record(record.state.event_id).state.delivery_status
        is DeliveryStatus.JOB_PUBLISHED
    )


def test_delivery_pump_keeps_artifacts_ready_when_rabbit_is_down(tmp_path):
    spool, record = _record(tmp_path)

    def unavailable():
        raise RuntimeError("rabbit unavailable")

    pump = DeliveryPump(
        dispatcher=OutboxDispatcher(spool=spool, store=_store()),
        publisher_factory=unavailable,
    )

    report = pump.run_once()

    assert report.uploaded_records == 1
    assert report.published_jobs == 0
    assert report.error_code == "RUNTIMEERROR"
    assert spool.open_record(record.state.event_id).state.delivery_status is DeliveryStatus.ARTIFACTS_READY


def test_delivery_pump_resumes_inference_publish_from_artifacts_ready(tmp_path):
    spool, record = _record(tmp_path)
    spool.advance_delivery(record.state.event_id, DeliveryStatus.ARTIFACTS_READY)
    transport = _Confirmed()
    pump = DeliveryPump(
        dispatcher=OutboxDispatcher(spool=spool, store=_store()),
        publisher_factory=lambda: FrozenJobPublisher(
            spool=spool, publisher=transport
        ),
    )

    report = pump.run_once()

    assert report.uploaded_records == 0
    assert report.published_jobs == 1
    assert transport.bodies == [record.frozen_job_bytes()]
    assert (
        spool.open_record(record.state.event_id).state.delivery_status
        is DeliveryStatus.JOB_PUBLISHED
    )


def test_delivery_pump_never_routes_terminal_result_to_inference_queue(tmp_path):
    spool, record = _record(tmp_path, reject_quality=True)
    publisher_factory_called = False

    def forbidden_publisher_factory():
        nonlocal publisher_factory_called
        publisher_factory_called = True
        raise AssertionError("terminal result must not create an inference publisher")

    pump = DeliveryPump(
        dispatcher=OutboxDispatcher(spool=spool, store=_store()),
        publisher_factory=forbidden_publisher_factory,
    )

    report = pump.run_once()

    assert publisher_factory_called is False
    assert report.error_code is None
    assert (
        spool.open_record(record.state.event_id).state.delivery_status
        is DeliveryStatus.TERMINAL_RESULT_DURABLE
    )
