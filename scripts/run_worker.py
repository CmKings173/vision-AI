#!/usr/bin/env python3
"""Run the Phase 2 resident inference worker with manual ACK semantics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from label_inspection.app import build_processor
from label_inspection.config import Settings
from label_inspection.messaging import (
    MessagePublishError,
    PikaConfirmedPublisher,
    RabbitTopology,
    RetryingWorkerMessageHandler,
    RetryPolicy,
    StructuredLifecycleLogger,
    TopologyConfig,
)
from label_inspection.storage import ArtifactKeyPolicy, MinioArtifactStore
from label_inspection.worker import InferenceWorker


def main() -> int:
    config = Settings()
    try:
        config.validate_phase2_worker()
        store = MinioArtifactStore.connect(
            endpoint=config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=config.minio_secure,
        )
        store.validate_bucket(config.artifact_bucket)
        processor = build_processor(config)
        worker = InferenceWorker(
            processor=processor,
            store=store,
            artifact_policy=ArtifactKeyPolicy(
                bucket=config.artifact_bucket,
                max_label_crop_bytes=config.max_label_crop_bytes,
            ),
            max_job_message_bytes=config.max_job_message_bytes,
            max_image_pixels=config.max_image_pixels,
        )
        print('{"status":"STARTUP","stage":"MODEL_WARMUP"}')
        worker.start()
    except Exception as exc:  # noqa: BLE001 - process boundary emits a safe code
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "stage": "WORKER_STARTUP",
                    "code": type(exc).__name__.upper(),
                },
                separators=(",", ":"),
            )
        )
        return 1

    try:
        import pika

        connection = pika.BlockingConnection(pika.URLParameters(config.rabbitmq_url))
        channel = connection.channel()
        topology = TopologyConfig.from_retry_delays(config.retry_delays_ms)
        RabbitTopology(topology).declare(channel)
        publisher = PikaConfirmedPublisher(channel)
    except Exception as exc:  # noqa: BLE001 - SDK connection boundary
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "stage": "RABBITMQ_CONNECT",
                    "code": type(exc).__name__.upper(),
                },
                separators=(",", ":"),
            )
        )
        return 1

    logger = StructuredLifecycleLogger()
    handler = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=publisher,
        topology=topology,
        policy=RetryPolicy.from_delays(config.retry_delays_ms),
        lifecycle_logger=logger,
        max_job_message_bytes=config.max_job_message_bytes,
    )
    fatal_publish_failure = False

    def on_message(ch, method, properties, body):
        nonlocal fatal_publish_failure
        message_id = getattr(properties, "message_id", None)
        correlation_id = getattr(properties, "correlation_id", None)
        headers = getattr(properties, "headers", None) or {}
        try:
            handler.handle(
                body,
                message_id=message_id,
                correlation_id=correlation_id,
                headers=headers,
                ack=lambda: ch.basic_ack(delivery_tag=method.delivery_tag),
            )
        except MessagePublishError:
            # Leave the current delivery unacked. Closing the channel below
            # returns it to RabbitMQ instead of losing it after failed handoff.
            fatal_publish_failure = True
            ch.stop_consuming()

    channel.basic_consume(
        queue=topology.queue,
        on_message_callback=on_message,
        auto_ack=False,
    )
    print(
        json.dumps(
            {
                "status": "WORKER_READY",
                "ocr_ready": True,
                "zxing_ready": True,
                "startup_ms": worker.startup_ms,
                "prefetch_count": topology.prefetch_count,
            },
            separators=(",", ":"),
        )
    )
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    except Exception:  # noqa: BLE001 - process boundary maps broker loss to exit
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "stage": "RABBITMQ_CONSUME",
                    "code": "BROKER_CONNECTION_LOST",
                },
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        if getattr(connection, "is_open", False):
            connection.close()
    return 1 if fatal_publish_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
