#!/usr/bin/env python3
"""Replay a local video into the bounded buffer and report top-K selection."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.selector import FrameSelector
from label_inspection.camera.video import capture_video_into_buffer
from label_inspection.config import settings
from label_inspection.runtime import unsupported_python_message
from label_inspection.smoke import SmokeExitCode, inspection_exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Local video path")
    parser.add_argument("--max-frames", type=int, default=20)
    parser.add_argument("--sample-every", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--buffer-size", type=int, default=8)
    parser.add_argument(
        "--preview-long-edge",
        type=int,
        default=settings.frame_preview_long_edge,
    )
    parser.add_argument("--detector", default="fixed-roi")
    parser.add_argument("--roi", help="x1,y1,x2,y2; normalized unless --roi-absolute")
    parser.add_argument("--roi-absolute", action="store_true")
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="Run one inspection on the selected buffer (OCR is not run per frame)",
    )
    args = parser.parse_args()
    version_error = unsupported_python_message()
    if version_error:
        print(json.dumps({"status": "ERROR", "reason": version_error}))
        return int(SmokeExitCode.USAGE_OR_RUNTIME)
    if args.sample_every < 1:
        parser.error("--sample-every must be >= 1")
    if args.max_frames < 1 or args.buffer_size < 1 or args.top_k < 1:
        parser.error("--max-frames, --buffer-size, and --top-k must be positive")

    try:
        import cv2
    except ImportError:
        print(json.dumps({"status": "ERROR", "reason": "OPENCV_NOT_INSTALLED"}))
        return int(SmokeExitCode.USAGE_OR_RUNTIME)

    capture = cv2.VideoCapture(args.source)
    if not capture.isOpened():
        print(json.dumps({"status": "FAIL", "reason": "VIDEO_OPEN_FAILED"}))
        return int(SmokeExitCode.FAILURE)

    buffer = FrameBuffer(max_size=args.buffer_size)
    selector = FrameSelector(
        top_k=args.top_k,
        preview_long_edge=args.preview_long_edge,
    )
    try:
        read_count, sampled_count = capture_video_into_buffer(
            capture,
            buffer,
            max_frames=args.max_frames,
            sample_every=args.sample_every,
        )
    finally:
        capture.release()

    if read_count == 0:
        print(json.dumps({"status": "FAIL", "reason": "NO_VIDEO_FRAME", "frames_read": 0}))
        return int(SmokeExitCode.FAILURE)

    packets = buffer.snapshot()
    selected = selector.select(packets)
    payload = {
        "status": "PASS",
        "frames_read": read_count,
        "frames_buffered": len(buffer),
        "frames_sampled": sampled_count,
        "selected_frame_ids": [packet.frame_id for packet in selected],
        "buffer_capacity": args.buffer_size,
    }
    if args.pipeline and packets:
        from label_inspection.app import build_pipeline
        config = replace(
            settings,
            detector=args.detector,
            label_roi=args.roi if args.roi is not None else settings.label_roi,
            roi_normalized=(
                not args.roi_absolute
                if args.roi is not None
                else settings.roi_normalized
            ),
            buffer_size=args.buffer_size,
            top_k=min(args.top_k, args.buffer_size),
        )
        logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
        try:
            result = build_pipeline(config).inspect_packets(packets)
        except ValueError as exc:
            payload["inspection"] = {
                "validation": {"status": "ERROR", "reasons": [str(exc)]}
            }
            payload["status"] = "ERROR"
        else:
            payload["inspection"] = result.to_dict()
            if result.validation.status == "ERROR":
                payload["status"] = "ERROR"
    print(
        json.dumps(
            payload
        )
    )
    inspection_status = (
        payload.get("inspection", {}).get("validation", {}).get("status")
        if args.pipeline
        else None
    )
    return inspection_exit_code(inspection_status, frames_read=read_count)


if __name__ == "__main__":
    raise SystemExit(main())
