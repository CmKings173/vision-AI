#!/usr/bin/env python3
"""Compare the previous full-frame selector cost with the preview selector."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from label_inspection.camera.selector import FrameSelector
from label_inspection.schemas import FramePacket


def legacy_full_frame_score(frame: object) -> float:
    array = np.asarray(frame)
    gray = array.mean(axis=2) if array.ndim == 3 else array
    brightness = float(gray.mean())
    contrast = float(gray.std())
    exposure = max(0.0, 1.0 - abs(brightness - 128.0) / 128.0)
    return exposure * 0.6 + min(contrast / 64.0, 1.0) * 0.4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--preview-long-edge", type=int, default=480)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if min(args.frames, args.width, args.height, args.repeats) < 1:
        parser.error("frames, dimensions, and repeats must be positive")

    frames = []
    for index in range(args.frames):
        frame = np.full(
            (args.height, args.width, 3),
            64 + index * 16,
            dtype=np.uint8,
        )
        frame[:, ::(16 + index * 2)] = 255
        frames.append(frame)
    captured_at = time.time()
    packets = [
        FramePacket(index, captured_at, frame, source="benchmark")
        for index, frame in enumerate(frames)
    ]
    before_selector = FrameSelector(
        top_k=min(3, args.frames),
        score_fn=legacy_full_frame_score,
    )
    after_selector = FrameSelector(
        top_k=min(3, args.frames),
        preview_long_edge=args.preview_long_edge,
    )

    # Warm imports and native kernels outside the measured loops.
    legacy_full_frame_score(frames[0])
    after_selector.select(packets, now=captured_at)

    before_ms, before_ids = _measure(before_selector, packets, captured_at, args.repeats)
    after_ms, after_ids = _measure(after_selector, packets, captured_at, args.repeats)
    before_median = statistics.median(before_ms)
    after_median = statistics.median(after_ms)
    print(
        json.dumps(
            {
                "frames": args.frames,
                "resolution": [args.width, args.height],
                "preview_long_edge": args.preview_long_edge,
                "repeats": args.repeats,
                "before_full_frame_median_ms": round(before_median, 3),
                "after_preview_median_ms": round(after_median, 3),
                "speedup": round(before_median / max(after_median, 0.001), 2),
                "before_selected_frame_ids": before_ids,
                "after_selected_frame_ids": after_ids,
            },
            sort_keys=True,
        )
    )
    return 0


def _measure(selector, packets, captured_at, repeats):
    durations = []
    selected_ids = []
    for _ in range(repeats):
        started = time.perf_counter()
        selected = selector.select(packets, now=captured_at)
        durations.append((time.perf_counter() - started) * 1000.0)
        selected_ids = [packet.frame_id for packet in selected]
    return durations, selected_ids


if __name__ == "__main__":
    raise SystemExit(main())
