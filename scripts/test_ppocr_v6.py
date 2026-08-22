#!/usr/bin/env python3
"""Run real PP-OCRv6 Transformers inference and a warm benchmark."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.ocr.ppocr_v6 import PPOCRV6TransformersAdapter


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999) - 1)))
    return ordered[index]


def _emit(payload: dict, code: int) -> int:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except (TypeError, ValueError):
        print(json.dumps({"status": "ERROR", "reason": "JSON_SERIALIZATION_FAILED"}))
        return 1
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--device", default="gpu:0")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--min-lines", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1 or args.warmup < 0 or args.min_lines < 0:
        return _emit({"status": "ERROR", "reason": "INVALID_ARGUMENT"}, 2)

    try:
        import cv2
    except ImportError:
        return _emit({"status": "ERROR", "reason": "OPENCV_NOT_INSTALLED"}, 2)
    image = cv2.imread(args.image)
    if image is None:
        return _emit({"status": "ERROR", "reason": "IMAGE_READ_FAILED"}, 1)

    adapter = PPOCRV6TransformersAdapter(device=args.device)
    load_started = time.perf_counter()
    first = adapter.recognize(image)
    load_elapsed = (time.perf_counter() - load_started) * 1000
    if not first.success:
        return _emit(
            {
                "status": "FAILED",
                "stage": "initial_inference",
                "load_and_first_inference_ms": load_elapsed,
                "ocr": first.to_dict(),
            },
            1,
        )
    if len(first.lines) < args.min_lines:
        return _emit(
            {"status": "FAILED", "stage": "no_text", "ocr": first.to_dict()},
            1,
        )

    for index in range(args.warmup):
        warmup = adapter.recognize(image)
        if not warmup.success:
            return _emit(
                {"status": "FAILED", "stage": f"warmup_{index + 1}", "ocr": warmup.to_dict()},
                1,
            )

    timings: list[float] = []
    last = first
    for index in range(args.runs):
        started = time.perf_counter()
        last = adapter.recognize(image)
        timings.append((time.perf_counter() - started) * 1000)
        if not last.success:
            return _emit(
                {"status": "FAILED", "stage": f"run_{index + 1}", "ocr": last.to_dict()},
                1,
            )

    payload = {
        "status": "SUCCESS",
        "input_path": str(Path(args.image).resolve()),
        "ocr": last.to_dict(),
        "model_load_and_first_inference_ms": load_elapsed,
        "model_load_excluded_from_benchmark": True,
        "benchmark": {
            "warmup_runs": args.warmup,
            "timed_runs": args.runs,
            "p50_ms": _percentile(timings, 0.50),
            "p95_ms": _percentile(timings, 0.95),
            "avg_ms": sum(timings) / len(timings),
        },
    }
    return _emit(payload, 0)


if __name__ == "__main__":
    raise SystemExit(main())
