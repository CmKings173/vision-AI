#!/usr/bin/env python3
"""Direct IP-camera URL -> ring buffer -> manual trigger -> inspection JSON."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.app import build_pipeline
from label_inspection.camera.acquisition import CameraAcquisition
from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.rtsp import RTSPCamera
from label_inspection.camera.security import mask_url_credentials, resolve_camera_source
from label_inspection.config import settings


def _emit(payload: dict, code: int) -> int:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (TypeError, ValueError):
        print(json.dumps({"status": "ERROR", "reason": "JSON_SERIALIZATION_FAILED"}))
        return 1
    return code


def _result_payload(source: str, result: object, telemetry: dict) -> dict:
    label = result.label
    barcode = result.barcode.to_dict()
    decoded_barcodes = getattr(result, "barcodes", [])
    barcode_items = [item.to_dict() for item in decoded_barcodes if item.value]
    if not barcode_items and barcode.get("value"):
        barcode_items = [barcode]
    return {
        "status": result.validation.status,
        "event_id": result.event_id,
        "camera_id": result.camera_id,
        "source": mask_url_credentials(source),
        "telemetry": telemetry,
        "selected_frame_id": result.frame_id,
        "selected_frame_timestamp": result.frame_timestamp,
        "label_bbox": list(label.bbox) if label is not None else None,
        "crop_bbox": list(result.crop_bbox) if result.crop_bbox is not None else None,
        "crop_score": result.candidate_score.to_dict()
        if result.candidate_score is not None
        else None,
        "quality": result.quality.to_dict(),
        "ocr": result.raw_ocr.to_dict(),
        "barcode": {
            "status": barcode.get("status"),
            "number_found": len(barcode_items),
            "items": barcode_items,
            "selected": barcode,
        },
        "fields": {key: value.to_dict() for key, value in result.extracted.items()},
        "validation": result.validation.to_dict(),
        "timings": dict(result.timing),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Direct RTSP/HTTP IP camera with Enter as the inspection trigger"
    )
    parser.add_argument("--source", help="Direct RTSP or HTTP camera URL")
    parser.add_argument("--camera-id")
    parser.add_argument("--roi", help="FixedROI x1,y1,x2,y2")
    parser.add_argument("--roi-absolute", action="store_true")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--required-fields", default="tracking_number,order_id")
    parser.add_argument("--connect-timeout-s", type=float, default=15.0)
    parser.add_argument(
        "--trigger-after-s",
        type=float,
        help="Use a timed trigger instead of waiting for Enter",
    )
    args = parser.parse_args()
    if args.connect_timeout_s <= 0 or (
        args.trigger_after_s is not None and args.trigger_after_s < 0
    ):
        return _emit({"status": "ERROR", "reason": "INVALID_ARGUMENT"}, 2)

    try:
        source = resolve_camera_source(args.source, settings.rtsp_url)
        config = replace(
            settings,
            camera_id=args.camera_id or settings.camera_id,
            detector="fixed-roi",
            label_roi=args.roi if args.roi is not None else settings.label_roi,
            roi_normalized=(not args.roi_absolute)
            if args.roi is not None
            else settings.roi_normalized,
            ocr_engine="ppocr_v6",
            ocr_backend="transformers",
            ocr_version="PP-OCRv6",
            ocr_device=args.device,
            barcode_engine="zxing",
            required_fields=tuple(
                item.strip().lower()
                for item in args.required_fields.split(",")
                if item.strip()
            ),
        )
        pipeline = build_pipeline(config)
    except Exception as exc:
        return _emit({"status": "ERROR", "stage": "setup", "reason": str(exc)}, 1)

    camera = RTSPCamera(
        source,
        open_timeout_ms=config.rtsp_open_timeout_ms,
        read_timeout_ms=config.rtsp_read_timeout_ms,
        max_frame_age_ms=config.max_frame_age_ms,
    )
    buffer = FrameBuffer(
        max_size=config.buffer_size,
        window_ms=config.buffer_window_ms,
    )
    acquisition = CameraAcquisition(camera, buffer)
    started_at = time.time()
    acquisition.start()
    try:
        first_packet = buffer.wait_for_frame(timeout_s=args.connect_timeout_s)
        if first_packet is None:
            health = camera.health
            return _emit(
                {
                    "status": "ERROR",
                    "stage": "camera_connect",
                    "reason": health.last_error or "NO_FRAME_RECEIVED",
                    "source": mask_url_credentials(source),
                    "camera_connected": health.connected,
                    "frames_received": acquisition.captured_count,
                    "camera_health": health.__dict__,
                },
                1,
            )

        health = camera.health
        print(
            f"Camera connected={health.connected}; frames_received="
            f"{acquisition.captured_count}; buffer_size={len(buffer)}",
            file=sys.stderr,
        )
        if args.trigger_after_s is None:
            input("Manual trigger: press Enter to inspect the newest buffered frames... ")
            trigger_mode = "manual_enter"
        else:
            print(
                f"Timed trigger in {args.trigger_after_s:.3f}s; press Ctrl-C to cancel.",
                file=sys.stderr,
            )
            time.sleep(args.trigger_after_s)
            trigger_mode = "timed"

        triggered_at = time.time()
        raw_packets = buffer.snapshot()
        fresh_packets = buffer.snapshot(
            now=triggered_at,
            monotonic_now=time.monotonic(),
        )
        health_at_trigger = camera.health
    except (EOFError, KeyboardInterrupt):
        return _emit(
            {
                "status": "ERROR",
                "stage": "manual_trigger",
                "reason": "TRIGGER_CANCELLED",
                "source": mask_url_credentials(source),
                "frames_received": acquisition.captured_count,
            },
            1,
        )
    finally:
        acquisition.stop()

    telemetry = {
        "capture_started_at": started_at,
        "triggered_at": triggered_at,
        "trigger_mode": trigger_mode,
        "camera_connected": health_at_trigger.connected,
        "frames_received": acquisition.captured_count,
        "buffered_frames": len(raw_packets),
        "fresh_frames_at_trigger": len(fresh_packets),
        "stale_frames_at_trigger": max(0, len(raw_packets) - len(fresh_packets)),
        "camera_health": health_at_trigger.__dict__,
    }
    if not fresh_packets:
        telemetry["reason"] = "NO_FRESH_FRAME_AT_TRIGGER"
        return _emit(
            {
                "status": "ERROR",
                "source": mask_url_credentials(source),
                "telemetry": telemetry,
            },
            1,
        )

    try:
        result = pipeline.inspect_packets(fresh_packets, camera_id=config.camera_id)
    except Exception as exc:
        telemetry["reason"] = "PIPELINE_RUNTIME_ERROR"
        telemetry["detail"] = str(exc)
        return _emit(
            {
                "status": "ERROR",
                "source": mask_url_credentials(source),
                "telemetry": telemetry,
            },
            1,
        )

    return _emit(
        _result_payload(source, result, telemetry),
        0 if result.validation.status in {"PASS", "REVIEW", "FAIL"} else 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
