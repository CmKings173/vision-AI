#!/usr/bin/env python3
"""Run the local V1 inspection pipeline for one image and print JSON."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.app import build_pipeline
from label_inspection.config import settings
from label_inspection.runtime import unsupported_python_message
from label_inspection.smoke import SmokeExitCode, inspection_exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Image path")
    parser.add_argument("--event-id")
    parser.add_argument("--camera-id")
    parser.add_argument("--roi", help="x1,y1,x2,y2; normalized unless --roi-absolute")
    parser.add_argument("--roi-absolute", action="store_true")
    parser.add_argument("--detector", default="fixed-roi")
    parser.add_argument("--detector-model")
    parser.add_argument("--detector-device")
    parser.add_argument("--detector-confidence", type=float)
    parser.add_argument("--detector-iou", type=float)
    parser.add_argument("--detector-imgsz", type=int)
    parser.add_argument("--detector-max-det", type=int)
    args = parser.parse_args()
    version_error = unsupported_python_message()
    if version_error:
        print(json.dumps({"status": "ERROR", "reason": version_error}))
        return int(SmokeExitCode.USAGE_OR_RUNTIME)

    try:
        import cv2
    except ImportError:
        print(json.dumps({"status": "ERROR", "reason": "OPENCV_NOT_INSTALLED"}))
        return int(SmokeExitCode.USAGE_OR_RUNTIME)

    image = cv2.imread(args.image)
    if image is None:
        print(json.dumps({"status": "ERROR", "reason": "IMAGE_READ_FAILED"}))
        return int(SmokeExitCode.FAILURE)

    config = replace(
        settings,
        detector=args.detector,
        detector_model=args.detector_model or settings.detector_model,
        detector_device=args.detector_device or settings.detector_device,
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
        label_roi=args.roi if args.roi is not None else settings.label_roi,
        roi_normalized=not args.roi_absolute if args.roi is not None else settings.roi_normalized,
    )
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    try:
        result = build_pipeline(config).inspect_frame(
            image,
            event_id=args.event_id,
            camera_id=args.camera_id,
        )
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}))
        return int(SmokeExitCode.FAILURE)
    except Exception:
        print(json.dumps({"status": "ERROR", "reason": "PIPELINE_START_ERROR"}))
        return int(SmokeExitCode.FAILURE)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return inspection_exit_code(result.validation.status)


if __name__ == "__main__":
    raise SystemExit(main())
