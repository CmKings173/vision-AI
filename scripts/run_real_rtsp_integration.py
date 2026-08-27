#!/usr/bin/env python3
"""Run the real RTSP -> bounded buffer -> FixedROI -> OCR/ZXing path."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.app import build_pipeline
from label_inspection.camera.acquisition import capture_into_buffer
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="RTSP URL; overrides VISION_RTSP_URL")
    parser.add_argument("--camera-id")
    parser.add_argument("--max-frames", type=int, default=30)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--roi", default="0,0,1,1")
    parser.add_argument("--roi-absolute", action="store_true")
    parser.add_argument(
        "--detector",
        choices=("fixed-roi", "yolo", "ultralytics"),
        default="fixed-roi",
    )
    parser.add_argument("--detector-model")
    parser.add_argument(
        "--detector-device",
        help="YOLO device; defaults to --device when --detector=yolo",
    )
    parser.add_argument("--detector-confidence", type=float)
    parser.add_argument("--detector-iou", type=float)
    parser.add_argument("--detector-imgsz", type=int)
    parser.add_argument("--detector-max-det", type=int)
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--required-fields", default="tracking_number,order_id")
    args = parser.parse_args()
    if args.max_frames < 1 or args.timeout_s <= 0:
        return _emit({"status": "ERROR", "reason": "INVALID_ARGUMENT"}, 2)

    try:
        source = resolve_camera_source(args.source, settings.rtsp_url)
    except ValueError as exc:
        return _emit({"status": "ERROR", "reason": str(exc)}, 2)
    required_fields = tuple(
        item.strip().lower() for item in args.required_fields.split(",") if item.strip()
    )
    config = replace(
        settings,
        camera_id=args.camera_id or settings.camera_id,
        detector=args.detector,
        detector_model=args.detector_model or settings.detector_model,
        detector_device=(
            args.detector_device
            or (args.device if args.detector in {"yolo", "ultralytics"} else settings.detector_device)
        ),
        detector_confidence=(
            settings.detector_confidence
            if args.detector_confidence is None
            else args.detector_confidence
        ),
        detector_iou=settings.detector_iou if args.detector_iou is None else args.detector_iou,
        detector_image_size=(
            settings.detector_image_size
            if args.detector_imgsz is None
            else args.detector_imgsz
        ),
        detector_max_det=(
            settings.detector_max_det
            if args.detector_max_det is None
            else args.detector_max_det
        ),
        label_roi=args.roi,
        roi_normalized=not args.roi_absolute,
        ocr_engine="ppocr_v6",
        ocr_backend="transformers",
        ocr_version="PP-OCRv6",
        ocr_device=args.device,
        required_fields=required_fields,
    )
    try:
        pipeline = build_pipeline(config)
    except Exception as exc:
        return _emit({"status": "ERROR", "stage": "pipeline_build", "reason": str(exc)}, 1)

    camera = RTSPCamera(
        source,
        open_timeout_ms=config.rtsp_open_timeout_ms,
        read_timeout_ms=config.rtsp_read_timeout_ms,
        max_frame_age_ms=config.max_frame_age_ms,
    )
    buffer = FrameBuffer(max_size=config.buffer_size, window_ms=config.buffer_window_ms)
    capture_started = time.perf_counter()
    try:
        captured_count = capture_into_buffer(
            camera,
            buffer,
            max_frames=args.max_frames,
            timeout_s=args.timeout_s,
        )
        health = camera.health
        all_packets = buffer.snapshot()
        fresh_packets = buffer.snapshot(now=time.time(), monotonic_now=time.monotonic())
    finally:
        camera.close()
    capture_elapsed = (time.perf_counter() - capture_started) * 1000

    base = {
        "source": mask_url_credentials(source),
        "frames_received": health.frames_received,
        "captured_count": captured_count,
        "buffered_frames": len(all_packets),
        "stale_frames": max(0, len(all_packets) - len(fresh_packets)),
        "dropped_by_bounded_buffer": max(0, captured_count - len(all_packets)),
        "camera_health": health.__dict__,
        "capture_ms": capture_elapsed,
    }
    if not fresh_packets:
        base.update({"status": "ERROR", "reason": "NO_FRESH_FRAME"})
        return _emit(base, 1)

    try:
        result = pipeline.inspect_packets(fresh_packets, camera_id=config.camera_id)
    except Exception as exc:
        base.update({"status": "ERROR", "reason": "PIPELINE_RUNTIME_ERROR", "detail": str(exc)})
        return _emit(base, 1)

    label = result.label
    barcode = result.barcode.to_dict()
    decoded_barcodes = getattr(result, "barcodes", [])
    barcode_items = [item.to_dict() for item in decoded_barcodes if item.value]
    if not barcode_items and barcode.get("value"):
        barcode_items = [barcode]
    base.update(
        {
            "status": result.validation.status,
            "selected_frame_id": result.frame_id,
            "label_bbox": list(label.bbox) if label is not None else None,
            "crop_bbox": list(result.crop_bbox) if result.crop_bbox is not None else None,
            "crop_score": result.candidate_score.to_dict()
            if result.candidate_score is not None
            else None,
            "ocr": result.raw_ocr.to_dict(),
            "barcode": {
                "status": barcode.get("status"),
                "number_found": len(barcode_items),
                "items": barcode_items,
            },
            "fields": {key: value.to_dict() for key, value in result.extracted.items()},
            "validation": result.validation.to_dict(),
            "timings": dict(result.timing),
        }
    )
    return _emit(base, 0 if result.validation.status in {"PASS", "REVIEW", "FAIL"} else 1)


if __name__ == "__main__":
    raise SystemExit(main())
