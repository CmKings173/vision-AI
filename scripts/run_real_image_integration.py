#!/usr/bin/env python3
"""Run the real FixedROI -> PP-OCRv6 -> ZXing image integration test."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.app import build_pipeline
from label_inspection.config import settings


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999) - 1)))
    return ordered[index]


def _payload(image_path: str, image: object, result: object) -> dict:
    shape = getattr(image, "shape", (0, 0))
    height = int(shape[0]) if len(shape) > 0 else 0
    width = int(shape[1]) if len(shape) > 1 else 0
    label = getattr(result, "label", None)
    quality = result.quality.to_dict()
    raw_ocr = result.raw_ocr.to_dict()
    barcode = result.barcode.to_dict()
    decoded_barcodes = getattr(result, "barcodes", [])
    items = [item.to_dict() for item in decoded_barcodes if item.value]
    if not items and barcode.get("value"):
        items = [barcode]
    return {
        "status": result.validation.status,
        "input": {
            "path": str(Path(image_path).resolve()),
            "width": width,
            "height": height,
        },
        "label_bbox": list(label.bbox) if label is not None else None,
        "crop_bbox": list(result.crop_bbox) if result.crop_bbox is not None else None,
        "quality": quality,
        "ocr": raw_ocr,
        "barcode": {
            "status": barcode.get("status"),
            "number_found": len(items),
            "items": items,
            "selected": barcode,
        },
        "fields": {key: value.to_dict() for key, value in result.extracted.items()},
        "validation": result.validation.to_dict(),
        "timings": dict(result.timing),
    }


def _emit(payload: dict) -> int:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (TypeError, ValueError):
        print(json.dumps({"status": "ERROR", "reason": "JSON_SERIALIZATION_FAILED"}))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--roi", default="0,0,1,1", help="FixedROI x1,y1,x2,y2")
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
    parser.add_argument(
        "--extraction-profile",
        default=None,
        help="Optional named extraction profile, e.g. dgx_spark_label",
    )
    parser.add_argument("--required-fields", default="tracking_number,order_id")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    if args.warmup < 0 or args.runs < 1:
        return _emit({"status": "ERROR", "reason": "INVALID_ARGUMENT"})

    try:
        import cv2
    except ImportError:
        return _emit({"status": "ERROR", "reason": "OPENCV_NOT_INSTALLED"})
    image = cv2.imread(args.image)
    if image is None:
        return _emit({"status": "ERROR", "reason": "IMAGE_READ_FAILED"})

    required_fields = tuple(
        item.strip().lower() for item in args.required_fields.split(",") if item.strip()
    )
    config = replace(
        settings,
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
        extraction_profile=args.extraction_profile or settings.extraction_profile,
    )
    try:
        pipeline = build_pipeline(config)
    except Exception as exc:
        return _emit({"status": "ERROR", "stage": "pipeline_build", "reason": str(exc)})

    first_started = time.perf_counter()
    first = pipeline.inspect_frame(image, event_id="REAL-IMAGE-INITIAL")
    first_elapsed = (time.perf_counter() - first_started) * 1000
    initial_payload = _payload(args.image, image, first)
    initial_payload["initial_total_ms_including_model_load"] = first_elapsed
    if first.validation.status != "PASS":
        initial_payload["benchmark"] = {"status": "STOPPED_IMAGE_NOT_PASS"}
        _emit(initial_payload)
        return 1

    for index in range(args.warmup):
        warmup = pipeline.inspect_frame(image, event_id=f"REAL-IMAGE-WARMUP-{index + 1}")
        if warmup.validation.status != "PASS":
            payload = _payload(args.image, image, warmup)
            payload["benchmark"] = {"status": "STOPPED_WARMUP_NOT_PASS"}
            _emit(payload)
            return 1

    ocr_timings: list[float] = []
    total_timings: list[float] = []
    barcode_timings: list[float] = []
    last = first
    for index in range(args.runs):
        last = pipeline.inspect_frame(image, event_id=f"REAL-IMAGE-RUN-{index + 1}")
        if last.validation.status != "PASS":
            payload = _payload(args.image, image, last)
            payload["benchmark"] = {"status": "STOPPED_TIMED_RUN_NOT_PASS"}
            _emit(payload)
            return 1
        ocr_timings.append(float(last.timing.get("ocr_ms", 0.0)))
        total_timings.append(float(last.timing.get("total_ms", 0.0)))
        barcode_timings.append(float(last.timing.get("barcode_ms", 0.0)))

    payload = _payload(args.image, image, last)
    payload["benchmark"] = {
        "warmup_runs": args.warmup,
        "timed_runs": args.runs,
        "model_load_excluded": True,
        "ocr_p50_ms": _percentile(ocr_timings, 0.50),
        "ocr_p95_ms": _percentile(ocr_timings, 0.95),
        "total_pipeline_p50_ms": _percentile(total_timings, 0.50),
        "total_pipeline_p95_ms": _percentile(total_timings, 0.95),
        "barcode_avg_ms": sum(barcode_timings) / len(barcode_timings),
        "barcode_p95_ms": _percentile(barcode_timings, 0.95),
    }
    return _emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
