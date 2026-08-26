"""Bounded confirmed retry and dead-letter handoff."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from ..contracts.core import require_uuid
from .observability import StructuredLifecycleLogger
from .publisher import ConfirmedPublisher
from .topology import TopologyConfig, retry_names_for_delays


@dataclass(frozen=True)
class RetryPolicy:
    delays_ms: tuple[int, ...] = (5_000, 30_000, 120_000)
    queue_names: tuple[str, ...] = (
        "vision.inspection.retry.5s.q",
        "vision.inspection.retry.30s.q",
        "vision.inspection.retry.120s.q",
    )
    routing_keys: tuple[str, ...] = (
        "inspection.retry.5s",
        "inspection.retry.30s",
        "inspection.retry.120s",
    )

    @classmethod
    def from_delays(cls, delays_ms: tuple[int, ...]) -> RetryPolicy:
        queues, routes = retry_names_for_delays(delays_ms)
        return cls(
            delays_ms=tuple(delays_ms),
            queue_names=queues,
            routing_keys=routes,
        )

    def __post_init__(self) -> None:
        if not (
            len(self.delays_ms) == len(self.queue_names) == len(self.routing_keys)
        ):
            raise ValueError("retry policy tuples must have equal length")
        if not self.delays_ms or any(delay < 1 for delay in self.delays_ms):
            raise ValueError("retry delays must be positive milliseconds")


class DeliveryDisposition(str, Enum):
    COMPLETED = "COMPLETED"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"


class MessageIdentityError(RuntimeError):
    code = "MESSAGE_IDENTITY_MISMATCH"
    retryable = False


class MessageTooLargeError(RuntimeError):
    code = "MESSAGE_TOO_LARGE"
    retryable = False


class RetryingWorkerMessageHandler:
    """ACK only after durable result or confirmed retry/DLQ transfer."""

    def __init__(
        self,
        *,
        worker,
        publisher: ConfirmedPublisher,
        topology: TopologyConfig | None = None,
        policy: RetryPolicy | None = None,
        lifecycle_logger: StructuredLifecycleLogger | None = None,
        max_job_message_bytes: int = 1024 * 1024,
    ) -> None:
        self.worker = worker
        self.publisher = publisher
        self.topology = topology or TopologyConfig()
        self.policy = policy or RetryPolicy()
        self.lifecycle_logger = lifecycle_logger
        if (
            isinstance(max_job_message_bytes, bool)
            or not isinstance(max_job_message_bytes, int)
            or max_job_message_bytes < 1
        ):
            raise ValueError("max_job_message_bytes must be a positive integer")
        self.max_job_message_bytes = max_job_message_bytes
        if (
            self.policy.delays_ms != self.topology.retry_delays_ms
            or self.policy.queue_names != self.topology.retry_queue_names
            or self.policy.routing_keys != self.topology.retry_routing_keys
        ):
            raise ValueError("retry policy must match the declared RabbitMQ topology")

    def handle(
        self,
        body: bytes,
        *,
        message_id: str | None,
        headers: Mapping[str, object] | None,
        ack,
        correlation_id: str | None = None,
    ) -> DeliveryDisposition:
        attempt = _attempt(headers)
        canonical_event_id = _dead_letter_event_id(body, message_id)
        try:
            if not isinstance(body, bytes) or len(body) > self.max_job_message_bytes:
                raise MessageTooLargeError(
                    "RabbitMQ inspection job exceeds the configured size limit."
                )
            canonical_event_id = _validate_message_identity(
                body,
                message_id=message_id,
                correlation_id=correlation_id,
            )
            report = self.worker.process_message(body)
            if not bool(getattr(report, "durable_result", False)):
                raise RuntimeError("worker returned a non-durable result")
        except Exception as exc:  # noqa: BLE001 - all failures need bounded routing
            retryable = bool(getattr(exc, "retryable", True))
            error_code = str(getattr(exc, "code", type(exc).__name__)).upper()
            if retryable and attempt < len(self.policy.delays_ms):
                self.publisher.publish(
                    exchange=self.topology.exchange,
                    routing_key=self.policy.routing_keys[attempt],
                    body=body,
                    event_id=canonical_event_id,
                    message_type="inspection-job.v1",
                    attempt=attempt + 1,
                    headers={
                        "attempt": attempt + 1,
                        "previous_error_code": error_code,
                        "retry_delay_ms": self.policy.delays_ms[attempt],
                    },
                )
                ack()
                self._log(
                    canonical_event_id,
                    stage="RETRY_HANDOFF",
                    status="CONFIRMED",
                    attempt=attempt,
                    next_attempt=attempt + 1,
                    error_code=error_code,
                )
                return DeliveryDisposition.RETRY

            exhausted = retryable and attempt >= len(self.policy.delays_ms)
            self.publisher.publish(
                exchange=self.topology.exchange,
                routing_key=self.topology.dead_routing_key,
                body=body,
                event_id=canonical_event_id,
                message_type="inspection-job.v1",
                attempt=attempt,
                headers={
                    "attempt": attempt,
                    "failure_error_code": error_code,
                    "failure_retryable": retryable,
                    "retry_exhausted": exhausted,
                },
            )
            ack()
            self._log(
                canonical_event_id,
                stage="DLQ_HANDOFF",
                status="CONFIRMED",
                attempt=attempt,
                error_code=error_code,
                retry_exhausted=exhausted,
            )
            return DeliveryDisposition.DEAD_LETTER

        ack()
        self._log(
            canonical_event_id,
            stage="RESULT_DURABLE",
            status="COMPLETED",
            attempt=attempt,
            artifact_download_ms=getattr(report, "artifact_download_ms", None),
            checksum_ms=getattr(report, "checksum_ms", None),
            image_decode_ms=getattr(report, "image_decode_ms", None),
            queue_wait_ms=getattr(report, "queue_wait_ms", None),
            result_persist_ms=getattr(report, "result_persist_ms", None),
            worker_total_ms=getattr(report, "worker_total_ms", None),
            end_to_end_ms=getattr(report, "end_to_end_ms", None),
        )
        return DeliveryDisposition.COMPLETED

    def _log(self, event_id: str, *, stage: str, status: str, **fields) -> None:
        if self.lifecycle_logger is not None:
            self.lifecycle_logger.emit(
                event_id=event_id,
                component="inference-worker",
                stage=stage,
                status=status,
                **fields,
            )


def _attempt(headers: Mapping[str, object] | None) -> int:
    value = 0 if headers is None else headers.get("attempt", 0)
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _validate_message_identity(
    body: bytes,
    *,
    message_id: str | None,
    correlation_id: str | None,
) -> str:
    try:
        if message_id is None:
            raise ValueError("message_id is required")
        transport_event_id = require_uuid(message_id, "message_id")
        if correlation_id is not None:
            correlation_event_id = require_uuid(correlation_id, "correlation_id")
            if correlation_event_id != transport_event_id:
                raise ValueError("correlation identity mismatch")
        if not isinstance(body, bytes):
            raise TypeError("message body must be bytes")
        payload = json.loads(body)
        if not isinstance(payload, Mapping):
            raise TypeError("message body must be an object")
        body_event_id = require_uuid(payload.get("event_id"), "event_id")
        if body_event_id != transport_event_id:
            raise ValueError("body identity mismatch")
        return transport_event_id
    except Exception as exc:
        raise MessageIdentityError(
            "Rabbit transport identity does not match the inspection job."
        ) from exc


def _dead_letter_event_id(body: bytes, message_id: str | None) -> str:
    """Select trace identity for a rejected envelope without processing it."""

    try:
        if message_id is not None:
            return require_uuid(message_id, "message_id")
        payload = json.loads(body)
        if isinstance(payload, Mapping):
            return require_uuid(payload.get("event_id"), "event_id")
    except (TypeError, ValueError):
        pass
    # The transport adapter should reject missing IDs before this fallback. It
    # remains stable and non-secret for malformed poison envelopes.
    return "00000000-0000-0000-0000-000000000000"
