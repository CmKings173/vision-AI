#!/usr/bin/env python3
"""Run a warmed, real-pipeline evaluation over a dataset split."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.app import build_pipeline
from label_inspection.config import settings
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.evaluation.dataset import validate_dataset
from label_inspection.evaluation.evaluator import DatasetEvaluator
from label_inspection.extraction.profiles import DGX_SPARK_LABEL_FIELDS


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a real label-inspection dataset split")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--split", default="smoke")
    parser.add_argument("--image-id")
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id", help="Optional immutable run directory name")
    parser.add_argument(
        "--min-condition-samples",
        type=int,
        default=5,
        help="Minimum verified target samples before a condition metric is sufficient",
    )
    parser.add_argument("--roi", help="Calibrated normalized FixedROI x1,y1,x2,y2")
    parser.add_argument("--roi-absolute", action="store_true")
    parser.add_argument("--rotate-deg", type=int, default=settings.camera_rotate_degrees)
    parser.add_argument(
        "--quality-observation",
        action="store_true",
        help="Run OCR/barcode after a production quality rejection for analysis only",
    )
    args = parser.parse_args()

    if args.rotate_deg not in {0, 90, 180, 270}:
        print(json.dumps({"status": "ERROR", "reason": "INVALID_ROTATION"}))
        return 2

    validation = validate_dataset(args.dataset)
    if not validation.ok:
        print(json.dumps({"status": "ERROR", "stage": "dataset_validation", "validation": validation.to_dict()}, ensure_ascii=False, indent=2))
        return 1

    roi = args.roi if args.roi is not None else settings.label_roi
    if not roi:
        print(json.dumps({"status": "ERROR", "stage": "setup", "reason": "CALIBRATED_ROI_REQUIRED"}))
        return 2
    try:
        parsed_roi = FixedROIDetector.parse_roi(roi)
        roi_normalized = not args.roi_absolute if args.roi is not None else settings.roi_normalized
        if roi_normalized and parsed_roi == (0.0, 0.0, 1.0, 1.0):
            raise ValueError("Full-frame ROI 0,0,1,1 is not accepted for DGX evaluation")
        config = replace(
            settings,
            detector="fixed-roi",
            label_roi=roi,
            roi_normalized=roi_normalized,
            ocr_engine="ppocr_v6",
            ocr_backend="transformers",
            ocr_version="PP-OCRv6",
            ocr_device=args.device,
            barcode_engine="zxing",
            extraction_profile="dgx_spark_label",
            required_fields=DGX_SPARK_LABEL_FIELDS,
        )
        pipeline = build_pipeline(config)
        evaluator = DatasetEvaluator(
            pipeline=pipeline,
            dataset=args.dataset,
            output=args.output,
            split=args.split,
            image_id=args.image_id,
            device=args.device,
            rotate_degrees=args.rotate_deg,
            roi_normalized=roi_normalized,
            quality_observation=args.quality_observation,
            min_condition_samples=args.min_condition_samples,
            run_id=args.run_id,
        )
        summary = evaluator.run()
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "stage": "initialization", "error_type": type(exc).__name__, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
