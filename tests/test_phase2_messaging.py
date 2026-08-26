import json
import time

import pytest

from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import DeliveryStatus, TriggerEvent
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.messaging import (
    FrozenJobPublisher,
    MessagePublishError,
    PikaConfirmedPublisher,
    RabbitTopology,
    TopologyConfig,
)
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import FramePacket
from label_inspection.station.dispatcher import OutboxDispatcher
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.spool import LocalSpool
from label_inspection.storage import InMemoryArtifactStore
from tests.fixtures.quality import sharp_label


class _Channel:
    def __init__(self, *, publish_result=True) -> None:
        self.calls = []
        self.publish_result = publish_result
        self.confirmed = False

    def exchange_declare(self, **kwargs):
        self.calls.append(("exchange", kwargs))

    def queue_declare(self, **kwargs):
        self.calls.append(("queue", kwargs))

    def queue_bind(self, **kwargs):
        self.calls.append(("bind", kwargs))

    def basic_qos(self, **kwargs):
        self.calls.append(("qos", kwargs))

    def confirm_delivery(self):
        self.confirmed = True

    def basic_publish(self, **kwargs):
        self.calls.append(("publish", kwargs))
        return self.publish_result


def _spooled_ready_job(tmp_path):
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=int(time.time() * 1000),
    )
    packet = FramePacket(
        frame_id=19,
        captured_at=time.time() - 0.1,
        frame=sharp_label(),
        source="rtsp",
        captured_monotonic=time.monotonic() - 0.1,
    )
    preparer = StationPreparer(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        selector=FrameSelector(top_k=1, score_fn=lambda image: 1.0),
        quality_checker=QualityChecker(
            min_width=1,
            min_height=1,
            min_brightness=0,
            max_brightness=255,
            min_sharpness=0,
            max_underexposed_ratio=1,
            max_overexposed_ratio=1,
            max_glare_ratio=1,
        ),
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        bbox_padding_ratio=0,
    )
    outcome = preparer.prepare_trigger([packet], trigger=trigger)
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(outcome, provenance={"profile": "dgx-v1"})
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    OutboxDispatcher(spool=spool, store=store).dispatch_record(record)
    return spool, spool.open_record(record.state.event_id)


def test_rabbit_topology_is_durable_bound_and_prefetch_one():
    channel = _Channel()
    config = TopologyConfig()

    RabbitTopology(config).declare(channel)

    exchanges = [payload for kind, payload in channel.calls if kind == "exchange"]
    queues = [payload for kind, payload in channel.calls if kind == "queue"]
    bindings = [payload for kind, payload in channel.calls if kind == "bind"]
    assert {item["exchange"] for item in exchanges} == {config.exchange}
    assert all(item["durable"] is True for item in exchanges + queues)
    assert {item["queue"] for item in queues} >= {config.queue, config.dlq}
    assert {item["routing_key"] for item in bindings} >= {
        config.process_routing_key,
        config.dead_routing_key,
    }
    assert ("qos", {"prefetch_count": 1}) in channel.calls


def test_pika_publisher_uses_confirms_mandatory_persistent_and_identity_headers():
    channel = _Channel()
    publisher = PikaConfirmedPublisher(
        channel,
        properties_factory=lambda **kwargs: kwargs,
    )

    publisher.publish(
        exchange="vision.inspection.x",
        routing_key="inspection.process",
        body=b'{"event_id":"id"}',
        event_id="e1f0cd13-7b8a-49ae-8ad7-d999356490e1",
        message_type="inspection-job.v1",
    )

    assert channel.confirmed is True
    call = next(payload for kind, payload in channel.calls if kind == "publish")
    assert call["mandatory"] is True
    assert call["body"] == b'{"event_id":"id"}'
    assert call["properties"]["delivery_mode"] == 2
    assert call["properties"]["message_id"] == call["properties"]["correlation_id"]


def test_frozen_job_publisher_sends_exact_spool_bytes_then_marks_published(tmp_path):
    spool, record = _spooled_ready_job(tmp_path)
    channel = _Channel()
    transport = PikaConfirmedPublisher(
        channel, properties_factory=lambda **kwargs: kwargs
    )
    publisher = FrozenJobPublisher(spool=spool, publisher=transport)
    frozen = record.frozen_job_bytes()

    receipt = publisher.publish_record(record)
    current = spool.open_record(record.state.event_id)

    call = next(payload for kind, payload in channel.calls if kind == "publish")
    assert call["body"] == frozen
    assert json.loads(call["body"])["event_id"] == record.state.event_id
    assert receipt.event_id == record.state.event_id
    assert receipt.publish_ms >= 0
    assert receipt.local_commit_to_published_ms >= 0
    assert current.state.delivery_status is DeliveryStatus.JOB_PUBLISHED


def test_nack_or_unroutable_publish_never_advances_spool_state(tmp_path):
    spool, record = _spooled_ready_job(tmp_path)
    channel = _Channel(publish_result=False)
    transport = PikaConfirmedPublisher(
        channel, properties_factory=lambda **kwargs: kwargs
    )

    with pytest.raises(MessagePublishError):
        FrozenJobPublisher(spool=spool, publisher=transport).publish_record(record)

    assert spool.open_record(record.state.event_id).state.delivery_status is DeliveryStatus.ARTIFACTS_READY
