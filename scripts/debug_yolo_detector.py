#!/usr/bin/env python3
"""Diagnose one saved detector input with the exact trained checkpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.detection.ultralytics_detector import UltralyticsLabelDetector


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline YOLO diagnosis; no OCR, barcode, or checkpoint fallback"
    )
    parser.add_argument("--image", required=True, help="Saved detector_input.jpg")
    parser.add_argument("--weights", required=True, help="Exact trained checkpoint")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--max-det", type=int, default=10)
    parser.add_argument("--output-json")
    args = parser.parse_args()

    try:
        import cv2

        image = cv2.imread(args.image)
        if image is None:
            raise RuntimeError("IMAGE_READ_FAILED")
        detector = UltralyticsLabelDetector(
            args.weights,
            device=args.device,
            confidence=args.confidence,
            iou=args.iou,
            image_size=args.imgsz,
            max_det=args.max_det,
        )
        candidates = detector.detect(image)
        payload = {
            "status": "HIT" if candidates else "MISS",
            "image": str(Path(args.image).expanduser().resolve()),
            "weights": str(Path(args.weights).expanduser().resolve()),
            "detections": [candidate.to_dict() for candidate in candidates],
            "detector_debug": detector.last_debug,
        }
    except Exception as exc:  # noqa: BLE001 - CLI must serialize runtime failures
        payload = {
            "status": "ERROR",
            "image": str(Path(args.image).expanduser().resolve()),
            "weights": str(Path(args.weights).expanduser().resolve()),
            "error": str(exc),
        }

    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output_json:
        output = Path(args.output_json).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized + "\n", encoding="utf-8")
    return 0 if payload["status"] in {"HIT", "MISS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
