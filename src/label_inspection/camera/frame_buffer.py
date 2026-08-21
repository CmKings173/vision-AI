"""Bounded, timestamp-aware frame ring buffer."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

from ..schemas import FramePacket


class FrameBuffer:
    """Keep only the newest frames and expose stale-frame filtering."""

    def __init__(self, max_size: int = 8, window_ms: int = 800) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if window_ms < 0:
            raise ValueError("window_ms must be >= 0")
        self.max_size = max_size
        self.window_ms = window_ms
        self._frames: deque[FramePacket] = deque(maxlen=max_size)
        self._condition = threading.Condition()

    def append(self, packet: FramePacket) -> None:
        with self._condition:
            self._frames.append(packet)
            self._condition.notify_all()

    def snapshot(
        self,
        *,
        now: Optional[float] = None,
        monotonic_now: Optional[float] = None,
    ) -> list[FramePacket]:
        with self._condition:
            frames = list(self._frames)
        if (now is None and monotonic_now is None) or self.window_ms == 0:
            return frames
        wall_now = time.time() if now is None else now
        max_age_seconds = self.window_ms / 1000.0
        return [
            packet
            for packet in frames
            if (
                monotonic_now is not None
                and packet.captured_monotonic is not None
                and monotonic_now - packet.captured_monotonic <= max_age_seconds
            )
            or (
                (monotonic_now is None or packet.captured_monotonic is None)
                and wall_now - packet.captured_at <= max_age_seconds
            )
        ]

    def latest(
        self,
        *,
        now: Optional[float] = None,
        monotonic_now: Optional[float] = None,
    ) -> Optional[FramePacket]:
        frames = self.snapshot(now=now, monotonic_now=monotonic_now)
        return frames[-1] if frames else None

    def wait_for_frame(self, timeout_s: Optional[float] = None) -> Optional[FramePacket]:
        deadline = None if timeout_s is None else time.monotonic() + max(timeout_s, 0.0)
        with self._condition:
            while not self._frames:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._frames[-1]

    def clear(self) -> None:
        with self._condition:
            self._frames.clear()

    def __len__(self) -> int:
        with self._condition:
            return len(self._frames)
