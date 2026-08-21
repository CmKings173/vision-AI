#!/usr/bin/env python3
"""Open an RTSP source, verify reconnect-safe reads, and print frame metadata."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from label_inspection.camera.acquisition import capture_into_buffer
from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.rtsp import RTSPCamera
from label_inspection.camera.security import mask_url_credentials, resolve_camera_source
from label_inspection.config import settings
from label_inspection.runtime import unsupported_python_message
from label_inspection.smoke import SmokeExitCode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="RTSP URL; overrides VISION_RTSP_URL")
    parser.add_argument("--max-frames", type=int, default=10)
    parser.add_argument("--timeout-s", type=float, default=15.0)
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

    safe_source = mask_url_credentials(source)
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    logging.info("RTSP smoke source=%s", safe_source)
    camera = RTSPCamera(
        source,
        open_timeout_ms=settings.rtsp_open_timeout_ms,
        read_timeout_ms=settings.rtsp_read_timeout_ms,
        max_frame_age_ms=settings.max_frame_age_ms,
    )
    buffer = FrameBuffer(max_size=settings.buffer_size, window_ms=settings.buffer_window_ms)
    try:
        received = capture_into_buffer(
            camera,
            buffer,
            max_frames=args.max_frames,
            timeout_s=args.timeout_s,
        )
        for packet in buffer.snapshot():
            shape = getattr(packet.frame, "shape", None)
            print(
                json.dumps(
                    {
                        "frame_id": packet.frame_id,
                        "captured_at": packet.captured_at,
                        "shape": list(shape) if shape is not None else None,
                        "source": packet.source,
                    }
                )
            )
    finally:
        camera.close()

    if received == 0:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "reason": camera.health.last_error or "NO_FRAME",
                    "source": safe_source,
                    "health": camera.health.__dict__,
                }
            )
        )
        return int(SmokeExitCode.FAILURE)
    print(json.dumps({"status": "PASS", "frames": received, "health": camera.health.__dict__}))
    return int(SmokeExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
