"""RabbitMQ topology contract for Phase 2 inspection jobs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopologyConfig:
    exchange: str = "vision.inspection.x"
    process_routing_key: str = "inspection.process"
    dead_routing_key: str = "inspection.dead"
    queue: str = "vision.inspection.q"
    dlq: str = "vision.inspection.dlq"
    prefetch_count: int = 1
    retry_delays_ms: tuple[int, ...] = (5_000, 30_000, 120_000)
    retry_queue_names: tuple[str, ...] = (
        "vision.inspection.retry.5s.q",
        "vision.inspection.retry.30s.q",
        "vision.inspection.retry.120s.q",
    )
    retry_routing_keys: tuple[str, ...] = (
        "inspection.retry.5s",
        "inspection.retry.30s",
        "inspection.retry.120s",
    )

    @classmethod
    def from_retry_delays(cls, delays_ms: tuple[int, ...]) -> TopologyConfig:
        queues, routes = retry_names_for_delays(delays_ms)
        return cls(
            retry_delays_ms=tuple(delays_ms),
            retry_queue_names=queues,
            retry_routing_keys=routes,
        )

    def __post_init__(self) -> None:
        for name in (
            "exchange",
            "process_routing_key",
            "dead_routing_key",
            "queue",
            "dlq",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.prefetch_count < 1:
            raise ValueError("prefetch_count must be >= 1")
        if not (
            len(self.retry_delays_ms)
            == len(self.retry_queue_names)
            == len(self.retry_routing_keys)
        ):
            raise ValueError("retry topology tuples must have equal length")
        if not self.retry_delays_ms or any(delay < 1 for delay in self.retry_delays_ms):
            raise ValueError("retry delays must be positive milliseconds")


class RabbitTopology:
    def __init__(self, config: TopologyConfig | None = None) -> None:
        self.config = config or TopologyConfig()

    def declare(self, channel) -> None:
        config = self.config
        channel.exchange_declare(
            exchange=config.exchange,
            exchange_type="direct",
            durable=True,
        )
        channel.queue_declare(
            queue=config.queue,
            durable=True,
            arguments={
                "x-dead-letter-exchange": config.exchange,
                "x-dead-letter-routing-key": config.dead_routing_key,
            },
        )
        channel.queue_declare(queue=config.dlq, durable=True)
        channel.queue_bind(
            exchange=config.exchange,
            queue=config.queue,
            routing_key=config.process_routing_key,
        )
        channel.queue_bind(
            exchange=config.exchange,
            queue=config.dlq,
            routing_key=config.dead_routing_key,
        )
        for queue, routing_key, delay_ms in zip(
            config.retry_queue_names,
            config.retry_routing_keys,
            config.retry_delays_ms,
        ):
            channel.queue_declare(
                queue=queue,
                durable=True,
                arguments={
                    "x-message-ttl": delay_ms,
                    "x-dead-letter-exchange": config.exchange,
                    "x-dead-letter-routing-key": config.process_routing_key,
                },
            )
            channel.queue_bind(
                exchange=config.exchange,
                queue=queue,
                routing_key=routing_key,
            )
        channel.basic_qos(prefetch_count=config.prefetch_count)


def retry_names_for_delays(
    delays_ms: tuple[int, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    labels = tuple(
        f"{delay // 1000}s" if delay % 1000 == 0 else f"{delay}ms"
        for delay in delays_ms
    )
    return (
        tuple(f"vision.inspection.retry.{label}.q" for label in labels),
        tuple(f"inspection.retry.{label}" for label in labels),
    )
