"""Confirmed RabbitMQ publishing and immutable spool-job handoff."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..contracts import DeliveryStatus, InspectionJob
from ..station.spool import LocalSpool, RecordType, SpoolRecord
from .topology import TopologyConfig


class MessagePublishError(RuntimeError):
    """A message was not durably confirmed or could not be routed."""


class ConfirmedPublisher(Protocol):
    def publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        body: bytes,
        event_id: str,
        message_type: str,
        attempt: int = 0,
        headers: dict[str, object] | None = None,
    ) -> None: ...


class PikaConfirmedPublisher:
    """Small Pika adapter enforcing confirms, mandatory routing and persistence."""

    def __init__(
        self,
        channel,
        *,
        properties_factory: Callable[..., object] | None = None,
    ) -> None:
        self.channel = channel
        if properties_factory is None:
            try:
                import pika
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "RabbitMQ support requires the phase2 optional dependencies."
                ) from exc
            properties_factory = pika.BasicProperties
        self._properties_factory = properties_factory
        try:
            self.channel.confirm_delivery()
        except Exception as exc:
            raise MessagePublishError(
                "RabbitMQ publisher confirms could not be enabled."
            ) from exc

    def publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        body: bytes,
        event_id: str,
        message_type: str,
        attempt: int = 0,
        headers: dict[str, object] | None = None,
    ) -> None:
        if not isinstance(body, bytes):
            raise TypeError("RabbitMQ body must be frozen bytes")
        message_headers: dict[str, object] = {"attempt": attempt}
        message_headers.update(headers or {})
        properties = self._properties_factory(
            content_type="application/json",
            content_encoding="utf-8",
            delivery_mode=2,
            message_id=event_id,
            correlation_id=event_id,
            type=message_type,
            timestamp=int(time.time()),
            headers=message_headers,
        )
        try:
            confirmed = self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=body,
                properties=properties,
                mandatory=True,
            )
        except Exception as exc:
            raise MessagePublishError(
                "RabbitMQ did not confirm a routable persistent message."
            ) from exc
        if confirmed is False:
            raise MessagePublishError(
                "RabbitMQ negatively acknowledged the persistent message."
            )


@dataclass(frozen=True)
class PublishReceipt:
    event_id: str
    already_published: bool
    publish_ms: float = 0.0
    local_commit_to_published_ms: int = 0


class FrozenJobPublisher:
    """Publish exact committed job bytes and advance only after confirmation."""

    def __init__(
        self,
        *,
        spool: LocalSpool,
        publisher: ConfirmedPublisher,
        topology: TopologyConfig | None = None,
    ) -> None:
        self.spool = spool
        self.publisher = publisher
        self.topology = topology or TopologyConfig()

    def publish_record(self, record: SpoolRecord) -> PublishReceipt:
        current = self.spool.open_record(record.state.event_id)
        if current.record_type is not RecordType.INFERENCE_JOB:
            raise MessagePublishError("Terminal spool records are never inference jobs.")
        if current.state.delivery_status is DeliveryStatus.JOB_PUBLISHED:
            return PublishReceipt(current.state.event_id, already_published=True)
        if current.state.delivery_status is not DeliveryStatus.ARTIFACTS_READY:
            raise MessagePublishError(
                "Inference job cannot publish before artifacts are durable."
            )
        frozen = current.frozen_job_bytes()
        try:
            payload = json.loads(frozen)
            validated = InspectionJob.from_dict(payload)
        except Exception as exc:
            raise MessagePublishError("Frozen job contract is invalid.") from exc
        if validated.event_id != current.state.event_id:
            raise MessagePublishError("Frozen job identity does not match spool state.")

        publish_started = time.perf_counter()
        self.publisher.publish(
            exchange=self.topology.exchange,
            routing_key=self.topology.process_routing_key,
            body=frozen,
            event_id=current.state.event_id,
            message_type=validated.schema_version,
        )
        published = self.spool.advance_delivery(
            current.state.event_id, DeliveryStatus.JOB_PUBLISHED
        )
        return PublishReceipt(
            current.state.event_id,
            already_published=False,
            publish_ms=(time.perf_counter() - publish_started) * 1000.0,
            local_commit_to_published_ms=max(
                0,
                published.state.updated_at_ms - published.state.created_at_ms,
            ),
        )

    def publish_pending(self) -> tuple[PublishReceipt, ...]:
        records = self.spool.scan_recovery().pending_records
        return tuple(
            self.publish_record(record)
            for record in records
            if record.record_type is RecordType.INFERENCE_JOB
            and record.state.delivery_status is DeliveryStatus.ARTIFACTS_READY
        )
