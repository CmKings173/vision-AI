import json
from dataclasses import dataclass

import pytest

from label_inspection.messaging import (
    DeliveryDisposition,
    MessagePublishError,
    RabbitTopology,
    RetryingWorkerMessageHandler,
    RetryPolicy,
    StructuredLifecycleLogger,
    TopologyConfig,
)
from label_inspection.storage import ArtifactIntegrityError, StorageError

EVENT_ID = "e1f0cd13-7b8a-49ae-8ad7-d999356490e1"
OTHER_EVENT_ID = "4da920de-84af-42d6-8183-0755e08d46c8"
BODY = json.dumps({"event_id": EVENT_ID}).encode("utf-8")


class _Channel:
    def __init__(self) -> None:
        self.calls = []

    def exchange_declare(self, **kwargs):
        self.calls.append(("exchange", kwargs))

    def queue_declare(self, **kwargs):
        self.calls.append(("queue", kwargs))

    def queue_bind(self, **kwargs):
        self.calls.append(("bind", kwargs))

    def basic_qos(self, **kwargs):
        self.calls.append(("qos", kwargs))


class _Publisher:
    def __init__(self, *, fail=False) -> None:
        self.calls = []
        self.fail = fail

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise MessagePublishError("simulated confirm failure")


@dataclass
class _Report:
    durable_result: bool = True


class _Worker:
    def __init__(self, outcome=None) -> None:
        self.outcome = outcome or _Report()
        self.calls = 0

    def process_message(self, body):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_retry_topology_declares_ttl_dlx_queues_for_5_30_120_seconds():
    channel = _Channel()
    config = TopologyConfig()

    RabbitTopology(config).declare(channel)

    queues = {
        payload["queue"]: payload
        for kind, payload in channel.calls
        if kind == "queue"
    }
    policy = RetryPolicy()
    assert set(policy.queue_names).issubset(queues)
    for queue, delay in zip(policy.queue_names, policy.delays_ms):
        arguments = queues[queue]["arguments"]
        assert arguments["x-message-ttl"] == delay
        assert arguments["x-dead-letter-exchange"] == config.exchange
        assert arguments["x-dead-letter-routing-key"] == config.process_routing_key


def test_custom_retry_policy_and_topology_share_generated_routes():
    policy = RetryPolicy.from_delays((1_000, 5_000))
    topology = TopologyConfig.from_retry_delays((1_000, 5_000))

    assert policy.routing_keys == topology.retry_routing_keys
    assert policy.queue_names == topology.retry_queue_names
    RetryingWorkerMessageHandler(
        worker=_Worker(),
        publisher=_Publisher(),
        topology=topology,
        policy=policy,
    )

    with pytest.raises(ValueError, match="topology"):
        RetryingWorkerMessageHandler(
            worker=_Worker(),
            publisher=_Publisher(),
            topology=topology,
            policy=RetryPolicy.from_delays((2_000,)),
        )


def test_retryable_failure_is_confirmed_to_next_ttl_queue_before_ack():
    worker = _Worker(StorageError("MinIO unavailable"))
    publisher = _Publisher()
    acked = []
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=publisher)

    disposition = handler.handle(
        BODY,
        message_id=EVENT_ID,
        headers={"attempt": 0},
        ack=lambda: acked.append("ack"),
    )

    assert disposition is DeliveryDisposition.RETRY
    assert publisher.calls[0]["body"] == BODY
    assert publisher.calls[0]["routing_key"] == RetryPolicy().routing_keys[0]
    assert publisher.calls[0]["headers"]["attempt"] == 1
    assert acked == ["ack"]


def test_failed_retry_confirm_never_acks_original_delivery():
    worker = _Worker(StorageError("MinIO unavailable"))
    publisher = _Publisher(fail=True)
    acked = []
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=publisher)

    with pytest.raises(MessagePublishError):
        handler.handle(
            BODY,
            message_id=EVENT_ID,
            headers={"attempt": 0},
            ack=lambda: acked.append("ack"),
        )

    assert acked == []


def test_nonretryable_integrity_failure_is_confirmed_to_dlq_then_acked():
    worker = _Worker(ArtifactIntegrityError("checksum mismatch"))
    publisher = _Publisher()
    acked = []
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=publisher)

    disposition = handler.handle(
        BODY,
        message_id=EVENT_ID,
        headers={"attempt": 0},
        ack=lambda: acked.append("ack"),
    )

    assert disposition is DeliveryDisposition.DEAD_LETTER
    assert publisher.calls[0]["routing_key"] == TopologyConfig().dead_routing_key
    assert publisher.calls[0]["headers"]["failure_retryable"] is False
    assert acked == ["ack"]


def test_retry_exhaustion_goes_to_final_dlq_not_unbounded_redelivery():
    worker = _Worker(StorageError("still unavailable"))
    publisher = _Publisher()
    acked = []
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=publisher)

    disposition = handler.handle(
        BODY,
        message_id=EVENT_ID,
        headers={"attempt": 3},
        ack=lambda: acked.append("ack"),
    )

    assert disposition is DeliveryDisposition.DEAD_LETTER
    assert publisher.calls[0]["routing_key"] == TopologyConfig().dead_routing_key
    assert publisher.calls[0]["headers"]["retry_exhausted"] is True
    assert acked == ["ack"]


def test_successful_durable_result_is_acked_without_republish():
    worker = _Worker()
    publisher = _Publisher()
    acked = []
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=publisher)

    disposition = handler.handle(
        BODY,
        message_id=EVENT_ID,
        headers={},
        ack=lambda: acked.append("ack"),
    )

    assert disposition is DeliveryDisposition.COMPLETED
    assert publisher.calls == []
    assert acked == ["ack"]


def test_matching_transport_and_body_event_identity_is_accepted():
    worker = _Worker()
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=_Publisher())

    disposition = handler.handle(
        BODY,
        message_id=EVENT_ID,
        headers={},
        ack=lambda: None,
        correlation_id=EVENT_ID,
    )

    assert disposition is DeliveryDisposition.COMPLETED
    assert worker.calls == 1


@pytest.mark.parametrize(
    ("message_id", "body_event_id", "correlation_id"),
    [
        (EVENT_ID, OTHER_EVENT_ID, EVENT_ID),
        (None, EVENT_ID, EVENT_ID),
        (EVENT_ID, EVENT_ID, OTHER_EVENT_ID),
    ],
)
def test_identity_violation_is_dlq_poison_before_worker_processing(
    message_id, body_event_id, correlation_id
):
    worker = _Worker()
    publisher = _Publisher()
    acked = []
    handler = RetryingWorkerMessageHandler(worker=worker, publisher=publisher)
    body = json.dumps({"event_id": body_event_id}).encode("utf-8")

    disposition = handler.handle(
        body,
        message_id=message_id,
        correlation_id=correlation_id,
        headers={},
        ack=lambda: acked.append("dlq-confirmed"),
    )

    assert disposition is DeliveryDisposition.DEAD_LETTER
    assert worker.calls == 0
    assert publisher.calls[0]["routing_key"] == TopologyConfig().dead_routing_key
    assert publisher.calls[0]["headers"]["failure_error_code"] == (
        "MESSAGE_IDENTITY_MISMATCH"
    )
    assert publisher.calls[0]["headers"]["failure_retryable"] is False
    assert acked == ["dlq-confirmed"]


def test_oversized_rabbit_body_is_rejected_before_json_or_worker(monkeypatch):
    worker = _Worker()
    publisher = _Publisher()
    handler = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=publisher,
        max_job_message_bytes=64,
    )
    body = b"{" + b"x" * 64
    json_called = False
    original_loads = json.loads

    def observe_loads(value, *args, **kwargs):
        nonlocal json_called
        if value is body:
            json_called = True
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr("label_inspection.messaging.retry.json.loads", observe_loads)

    disposition = handler.handle(
        body,
        message_id=EVENT_ID,
        correlation_id=EVENT_ID,
        headers={},
        ack=lambda: None,
    )

    assert disposition is DeliveryDisposition.DEAD_LETTER
    assert publisher.calls[0]["headers"]["failure_error_code"] == (
        "MESSAGE_TOO_LARGE"
    )
    assert worker.calls == 0
    assert json_called is False


def test_structured_lifecycle_log_is_one_line_traceable_and_secret_safe():
    lines = []
    logger = StructuredLifecycleLogger(sink=lines.append)

    logger.emit(
        event_id=EVENT_ID,
        component="inference-worker",
        stage="RESULT_PERSIST",
        status="COMPLETED",
        attempt=1,
        duration_ms=12.5,
    )

    assert len(lines) == 1
    assert "\n" not in lines[0]
    payload = json.loads(lines[0])
    assert payload["event_id"] == EVENT_ID
    assert payload["timestamp_ms"] >= 0
    assert payload["duration_ms"] == 12.5
    with pytest.raises(ValueError, match="sensitive"):
        logger.emit(
            event_id=EVENT_ID,
            component="worker",
            stage="unsafe",
            status="ERROR",
            secret_key="must-not-log",
        )
