#!/usr/bin/env python3
"""Capture a bounded RTSP window and run one local inspection."""

from __future__ import annotations

import argparse
import json
import logging
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
from label_inspection.runtime import unsupported_python_message
from label_inspection.smoke import SmokeExitCode, inspection_exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="RTSP URL; overrides VISION_RTSP_URL")
    parser.add_argument("--camera-id", default=None)
    parser.add_argument("--max-frames", type=int, default=30)
    parser.add_argument("--timeout-s", type=float, default=15.0)
    parser.add_argument("--detector", default="fixed-roi")
    parser.add_argument("--roi", help="x1,y1,x2,y2; normalized unless --roi-absolute")
    parser.add_argument("--roi-absolute", action="store_true")
    args = parser.parse_args()
    if args.max_frames < 1 or args.timeout_s <= 0:
        parser.error("--max-frames and --timeout-s must be positive")
    version_error = unsupported_python_message()
    if version_error:
        print(json.dumps({"status": "ERROR", "reason": version_error}))
        return int(SmokeExitCode.USAGE_OR_RUNTIME)

    try:
        source = resolve_camera_source(args.source, settings.rtsp_url)
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}))
        return int(SmokeExitCode.USAGE_OR_RUNTIME)

    config = replace(
        settings,
        camera_id=args.camera_id or settings.camera_id,
        detector=args.detector,
        label_roi=args.roi if args.roi is not None else settings.label_roi,
        roi_normalized=not args.roi_absolute if args.roi is not None else settings.roi_normalized,
    )
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    logging.info("RTSP inspection source=%s", mask_url_credentials(source))
    camera = RTSPCamera(
        source,
        open_timeout_ms=config.rtsp_open_timeout_ms,
        read_timeout_ms=config.rtsp_read_timeout_ms,
        max_frame_age_ms=config.max_frame_age_ms,
    )
    buffer = FrameBuffer(max_size=config.buffer_size, window_ms=config.buffer_window_ms)
    try:
        captured_count = capture_into_buffer(
            camera,
            buffer,
            max_frames=args.max_frames,
            timeout_s=args.timeout_s,
        )
    finally:
        camera.close()

    packets = buffer.snapshot(now=time.time(), monotonic_now=time.monotonic())
    if not packets:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": camera.health.last_error or "NO_FRESH_FRAME",
                    "source": mask_url_credentials(source),
                    "captured_count": captured_count,
                    "health": camera.health.__dict__,
                }
            )
        )
        return int(SmokeExitCode.FAILURE)
    try:
        result = build_pipeline(config).inspect_packets(packets, camera_id=config.camera_id)
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "reason": str(exc)}))
        return int(SmokeExitCode.FAILURE)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return inspection_exit_code(result.validation.status, frames_read=captured_count)


if __name__ == "__main__":
    raise SystemExit(main())
