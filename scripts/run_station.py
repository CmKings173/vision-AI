#!/usr/bin/env python3
"""Run the Phase 2 station service (capture/preparation/outbox only)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from label_inspection.app import build_local_spool, build_station_preparer
from label_inspection.camera.acquisition import CameraAcquisition
from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.rtsp import RTSPCamera
from label_inspection.camera.security import resolve_camera_source
from label_inspection.config import Settings
from label_inspection.extraction.profiles import (
    DGX_SPARK_MAPPING_SUMMARY,
    DGX_SPARK_PROFILE_VERSION,
    DGX_SPARK_SEMANTIC_BLOCKERS,
    normalize_profile,
)
from label_inspection.messaging import (
    FrozenJobPublisher,
    PikaConfirmedPublisher,
    RabbitTopology,
    StructuredLifecycleLogger,
    TopologyConfig,
)
from label_inspection.station.controller import StationController, StationTriggerFailure
from label_inspection.station.dispatcher import OutboxDispatcher
from label_inspection.station.service import DeliveryPump, StationService
from label_inspection.station.spool import SpoolCapacityError
from label_inspection.storage import DeferredArtifactStore, MinioArtifactStore


class _PublisherSession:
    def __init__(self, connection, publisher: FrozenJobPublisher) -> None:
        self.connection = connection
        self.publisher = publisher

    def publish_pending(self):
        return self.publisher.publish_pending()

    def close(self) -> None:
        if getattr(self.connection, "is_open", False):
            self.connection.close()


@dataclass(frozen=True)
class StationRuntime:
    service: StationService
    camera: RTSPCamera
    spool: object
    delivery_pump: DeliveryPump


def build_station_runtime(config: Settings, source: str) -> StationRuntime:
    """Compose capture/local-spool first; defer network delivery to the pump."""

    spool = build_local_spool(config)
    preparer = build_station_preparer(config)

    def connect_minio():
        return MinioArtifactStore.connect(
            endpoint=config.minio_endpoint,
            access_key=config.minio_access_key,
            secret_key=config.minio_secret_key,
            secure=config.minio_secure,
        )

    store = DeferredArtifactStore(
        bucket=config.artifact_bucket,
        store_factory=connect_minio,
    )
    frame_buffer = FrameBuffer(
        max_size=config.buffer_size,
        window_ms=config.buffer_window_ms,
    )
    camera = RTSPCamera(
        source,
        open_timeout_ms=config.rtsp_open_timeout_ms,
        read_timeout_ms=config.rtsp_read_timeout_ms,
        max_frame_age_ms=config.max_frame_age_ms,
    )
    acquisition = CameraAcquisition(camera, frame_buffer)
    normalized_profile = normalize_profile(config.extraction_profile)
    profile_version = (
        DGX_SPARK_PROFILE_VERSION
        if normalized_profile in {"dgx_spark", "dgx_spark_label"}
        else None
    )
    requested_profile = (
        "dgx_spark_label"
        if normalized_profile in {"dgx_spark", "dgx_spark_label"}
        else None
    )
    locator = _detector_provenance(preparer.detector)
    controller = StationController(
        frame_buffer=frame_buffer,
        preparer=preparer,
        spool=spool,
        station_id=config.station_id,
        camera_id=config.camera_id,
        provenance={
            "requested_profile": (
                None
                if requested_profile is None
                else {
                    "name": requested_profile,
                    "version": profile_version,
                }
            ),
            "producer": {
                "semantic_blockers": (
                    DGX_SPARK_SEMANTIC_BLOCKERS
                    if normalized_profile in {"dgx_spark", "dgx_spark_label"}
                    else {}
                ),
                "mapping_summary": (
                    DGX_SPARK_MAPPING_SUMMARY
                    if normalized_profile in {"dgx_spark", "dgx_spark_label"}
                    else {}
                ),
                "locator_version": locator["version"],
                "locator": locator,
            },
        },
    )
    delivery_pump = DeliveryPump(
        dispatcher=OutboxDispatcher(spool=spool, store=store),
        publisher_factory=_publisher_factory(config, spool),
        interval_s=config.dispatch_interval_s,
        lifecycle_logger=StructuredLifecycleLogger(),
    )
    service = StationService(
        acquisition=acquisition,
        frame_buffer=frame_buffer,
        controller=controller,
        delivery_pump=delivery_pump,
    )
    return StationRuntime(
        service=service,
        camera=camera,
        spool=spool,
        delivery_pump=delivery_pump,
    )


def _detector_provenance(detector) -> dict[str, object]:
    """Describe the active locator without leaking host-specific model paths."""

    name = str(getattr(detector, "name", type(detector).__name__))
    support_level = str(getattr(detector, "support_level", "UNKNOWN"))
    normalized_name = name.strip().lower().replace("-", "").replace("_", "")
    if normalized_name == "fixedroi":
        return {
            "type": "fixed_roi",
            "version": "fixed-roi.v1",
            "support_level": support_level,
            "roi": [float(value) for value in detector.roi],
            "normalized": bool(detector.normalized),
            "confidence": float(detector.confidence),
        }
    if normalized_name == "contour":
        return {
            "type": "contour",
            "version": "contour.v1",
            "support_level": support_level,
            "min_area_ratio": float(detector.min_area_ratio),
            "max_candidates": int(detector.max_candidates),
            "threshold": int(detector.threshold),
        }

    metadata = getattr(detector, "runtime_metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError("detector runtime_metadata must be a mapping")
    if normalized_name == "ultralytics" or "model_sha256" in metadata:
        stable_keys = (
            "model_name",
            "model_version",
            "model_sha256",
            "configured_device",
            "actual_device",
            "ultralytics_version",
            "torch_version",
            "cuda_version",
            "actual_cuda_device_name",
            "confidence",
            "iou",
            "imgsz",
            "max_det",
            "class_mapping",
            "expected_class",
        )
        return {
            "type": "ultralytics_yolo",
            "version": "ultralytics-yolo.v1",
            "support_level": support_level,
            **{key: metadata[key] for key in stable_keys if key in metadata},
        }

    raise ValueError(f"unsupported station detector provenance: {name}")


def _publisher_factory(config: Settings, spool):
    def create():
        connection = None
        try:
            try:
                import pika
            except ImportError as exc:
                raise RuntimeError(
                    "Phase 2 station requires the optional phase2 dependencies."
                ) from exc
            connection = pika.BlockingConnection(
                pika.URLParameters(config.rabbitmq_url)
            )
            channel = connection.channel()
            topology = TopologyConfig.from_retry_delays(config.retry_delays_ms)
            RabbitTopology(topology).declare(channel)
            transport = PikaConfirmedPublisher(channel)
            return _PublisherSession(
                connection,
                FrozenJobPublisher(
                    spool=spool,
                    publisher=transport,
                    topology=topology,
                ),
            )
        except Exception:
            if connection is not None and getattr(connection, "is_open", False):
                try:
                    connection.close()
                except Exception:  # noqa: BLE001,S110 - best-effort SDK cleanup
                    pass
            raise

    return create


def _station_config(base: Settings, args: argparse.Namespace) -> Settings:
    """Apply station CLI overrides without forcing a detector choice."""

    return replace(
        base,
        detector=args.detector or base.detector,
        detector_model=args.detector_model or base.detector_model,
        detector_device=args.detector_device or base.detector_device,
        detector_confidence=(
            base.detector_confidence
            if getattr(args, "detector_confidence", None) is None
            else args.detector_confidence
        ),
        detector_iou=(
            base.detector_iou
            if getattr(args, "detector_iou", None) is None
            else args.detector_iou
        ),
        detector_image_size=(
            base.detector_image_size
            if getattr(args, "detector_imgsz", None) is None
            else args.detector_imgsz
        ),
        detector_max_det=(
            base.detector_max_det
            if getattr(args, "detector_max_det", None) is None
            else args.detector_max_det
        ),
        label_roi=args.roi or base.label_roi,
        camera_rotate_degrees=(
            base.camera_rotate_degrees
            if args.rotate_deg is None
            else args.rotate_deg
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 persistent station service")
    parser.add_argument("--source", help="Direct RTSP/HTTP camera URL")
    parser.add_argument("--roi", help="Normalized FixedROI x1,y1,x2,y2")
    parser.add_argument(
        "--detector",
        choices=("fixed-roi", "yolo", "ultralytics"),
        help="Detector override; defaults to VISION_DETECTOR",
    )
    parser.add_argument("--detector-model", help="YOLO model path")
    parser.add_argument("--detector-device", help="YOLO device, e.g. cuda:0")
    parser.add_argument("--detector-confidence", type=float)
    parser.add_argument("--detector-iou", type=float)
    parser.add_argument("--detector-imgsz", type=int)
    parser.add_argument("--detector-max-det", type=int)
    parser.add_argument("--rotate-deg", type=int)
    parser.add_argument(
        "--triggers",
        type=int,
        default=0,
        help="Number of manual triggers; 0 keeps running until Ctrl-C",
    )
    parser.add_argument("--connect-timeout-s", type=float, default=20.0)
    args = parser.parse_args()
    if args.triggers < 0 or args.connect_timeout_s <= 0:
        print('{"status":"ERROR","code":"INVALID_ARGUMENT"}')
        return 2

    base = Settings()
    try:
        source = resolve_camera_source(args.source, base.rtsp_url)
        config = _station_config(base, args)
        config.validate_phase2_station()
        runtime = build_station_runtime(config, source)
    except Exception as exc:  # noqa: BLE001 - process boundary emits a safe code
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "stage": "SETUP",
                    "code": type(exc).__name__.upper(),
                },
                separators=(",", ":"),
            )
        )
        return 1

    service = runtime.service
    camera = runtime.camera
    spool = runtime.spool

    completed = 0
    service.start()
    try:
        if not service.wait_ready(timeout_s=args.connect_timeout_s):
            print('{"status":"ERROR","stage":"CAMERA","code":"NOT_READY"}')
            return 1
        print(
            json.dumps(
                {
                    "status": "SYSTEM_READY",
                    "station_id": config.station_id,
                    "camera_id": config.camera_id,
                    "spool_root": str(spool.root),
                    "capture_ready": True,
                    "delivery_health": runtime.delivery_pump.delivery_health,
                },
                separators=(",", ":"),
            )
        )
        while args.triggers == 0 or completed < args.triggers:
            input("ENTER to inspect (Ctrl-C to stop): ")
            try:
                report = service.trigger()
            except SpoolCapacityError as exc:
                print(
                    json.dumps(
                        {
                            "status": "TRIGGER_REJECTED",
                            "code": exc.code,
                            "retryable": exc.retryable,
                        },
                        separators=(",", ":"),
                    )
                )
                continue
            except StationTriggerFailure as exc:
                print(
                    json.dumps(
                        {
                            "status": "LOCAL_COMMIT_ERROR",
                            "event_id": exc.event_id,
                            "trigger_id": exc.trigger_id,
                            "error": exc.error.to_dict(),
                        },
                        separators=(",", ":"),
                    )
                )
                continue
            completed += 1
            print(
                json.dumps(
                    {
                        "status": "LOCAL_COMMIT",
                        "event_id": report.event_id,
                        "trigger_id": report.trigger_id,
                        "record_type": report.record.record_type.value,
                        "delivery_status": report.record.state.delivery_status.value,
                        "spool_write_ms": report.spool_write_ms,
                        "trigger_to_local_commit_ms": report.trigger_to_local_commit_ms,
                        "delivery_health": runtime.delivery_pump.delivery_health,
                    },
                    separators=(",", ":"),
                )
            )
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        service.stop(
            timeout_s=max(2.5, config.rtsp_read_timeout_ms / 1000.0 + 0.5)
        )
        camera.wait_closed(
            timeout_s=max(2.5, config.rtsp_read_timeout_ms / 1000.0 + 0.5)
        )
    print(
        json.dumps(
            {"status": "STOPPED", "triggers_completed": completed},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
