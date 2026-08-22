#!/usr/bin/env python3
"""Run a real ZXing-C++ decode against one image and emit JSON."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.barcode.zxing import ZXingBarcodeDecoder


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999) - 1)))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--min-codes", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1 or args.min_codes < 0:
        print(json.dumps({"status": "ERROR", "reason": "INVALID_ARGUMENT"}))
        return 2

    try:
        import cv2
    except ImportError:
        print(json.dumps({"status": "ERROR", "reason": "OPENCV_NOT_INSTALLED"}))
        return 2
    image = cv2.imread(args.image)
    if image is None:
        print(json.dumps({"status": "ERROR", "reason": "IMAGE_READ_FAILED"}))
        return 1

    decoder = ZXingBarcodeDecoder(use_variants=True)
    timings: list[float] = []
    results = []
    for _ in range(args.runs):
        started = time.perf_counter()
        results = decoder.decode(image)
        timings.append((time.perf_counter() - started) * 1000)

    items = []
    for item in results:
        if not item.value:
            continue
        serialized = item.to_dict()
        serialized["text"] = serialized["value"]
        items.append(serialized)
    payload = {
        "status": "SUCCESS" if len(items) >= args.min_codes else "FAILED",
        "input_path": str(Path(args.image).resolve()),
        "number_found": len(items),
        "items": items,
        "latency_ms": timings[-1],
        "benchmark": {
            "runs": args.runs,
            "avg_ms": sum(timings) / len(timings),
            "p50_ms": _percentile(timings, 0.50),
            "p95_ms": _percentile(timings, 0.95),
        },
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        print(json.dumps({"status": "ERROR", "reason": "JSON_SERIALIZATION_FAILED"}))
        return 1
    print(encoded)
    return 0 if payload["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
