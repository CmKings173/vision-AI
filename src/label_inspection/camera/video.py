"""Local video replay helpers kept separate from blocking RTSP acquisition."""

from __future__ import annotations

import time
from typing import Callable

from ..schemas import FramePacket
from .frame_buffer import FrameBuffer


def capture_video_into_buffer(
    capture,
    buffer: FrameBuffer,
    *,
    max_frames: int,
    sample_every: int = 1,
    wall_clock: Callable[[], float] = time.time,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> tuple[int, int]:
    """Return (frames_read, frames_sampled); zero is an explicit failure upstream."""

    if max_frames < 1:
        raise ValueError("max_frames must be >= 1")
    if sample_every < 1:
        raise ValueError("sample_every must be >= 1")
    read_count = 0
    sampled_count = 0
    while read_count < max_frames:
        ok, frame = capture.read()
        if not ok or frame is None:
            break
        if read_count % sample_every == 0:
            buffer.append(
                FramePacket(
                    frame_id=read_count,
                    captured_at=wall_clock(),
                    frame=frame,
                    source="video",
                    captured_monotonic=monotonic_clock(),
                )
            )
            sampled_count += 1
        read_count += 1
    return read_count, sampled_count
