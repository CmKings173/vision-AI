"""Top-K frame selection without running OCR on every buffered frame."""

from __future__ import annotations

import time
from typing import Callable, Iterable, Optional

from ..schemas import FramePacket
from .frame_buffer import FrameBuffer


ScoreFn = Callable[[object], float]


def _preview_frame(frame: object, long_edge: int):
    """Return a read-only-use preview; the original frame is never modified."""

    import numpy as np

    array = np.asarray(frame)
    if array.size == 0 or array.ndim < 2:
        return array
    height, width = array.shape[:2]
    largest = max(height, width)
    if largest <= long_edge:
        return array
    scale = long_edge / float(largest)
    target = (max(1, round(width * scale)), max(1, round(height * scale)))
    try:
        import cv2

        return cv2.resize(array, target, interpolation=cv2.INTER_AREA)
    except (ImportError, TypeError, ValueError, AttributeError):
        step = max(1, (largest + long_edge - 1) // long_edge)
        return array[::step, ::step]


def _default_score(frame: object, preview_long_edge: int = 480) -> float:
    """Score brightness and sharpness on a bounded preview, never full 4K."""

    try:
        import numpy as np

        array = _preview_frame(frame, preview_long_edge)
        if array.size == 0:
            return 0.0
        if array.ndim == 3:
            try:
                import cv2

                gray = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
            except (ImportError, TypeError, ValueError, AttributeError):
                gray = array.mean(axis=2)
        else:
            gray = array
        brightness = float(gray.mean())
        exposure = max(0.0, 1.0 - abs(brightness - 128.0) / 128.0)
        try:
            import cv2

            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except (ImportError, TypeError, ValueError, AttributeError):
            gray_float = np.asarray(gray, dtype=float)
            horizontal = np.diff(gray_float, axis=1)
            vertical = np.diff(gray_float, axis=0)
            sharpness = float(horizontal.var() + vertical.var())
        sharpness_score = sharpness / (sharpness + 100.0) if sharpness > 0 else 0.0
        return exposure * 0.35 + sharpness_score * 0.65
    except (ImportError, TypeError, ValueError, AttributeError):
        shape = getattr(frame, "shape", ())
        if len(shape) >= 2:
            return float(min(shape[0] * shape[1], 10_000_000)) / 10_000_000
        return 0.0


class FrameSelector:
    """Select recent, high-quality frames for detection/fallback.

    The selector returns at most ``top_k`` packets.  The inspection pipeline
    decides how many of those it actually attempts; the default is one OCR
    attempt, not OCR over the whole top-K set.
    """

    def __init__(
        self,
        top_k: int = 3,
        *,
        score_fn: Optional[ScoreFn] = None,
        max_frame_age_ms: int = 1000,
        preview_long_edge: int = 480,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self.top_k = top_k
        self.score_fn = score_fn
        if max_frame_age_ms < 1:
            raise ValueError("max_frame_age_ms must be >= 1")
        self.max_frame_age_ms = max_frame_age_ms
        if not 320 <= preview_long_edge <= 640:
            raise ValueError("preview_long_edge must be between 320 and 640")
        self.preview_long_edge = preview_long_edge

    def select(
        self,
        packets: Iterable[FramePacket],
        *,
        now: Optional[float] = None,
        monotonic_now: Optional[float] = None,
    ) -> list[FramePacket]:
        current_time = time.time() if now is None else now
        current_monotonic = time.monotonic() if monotonic_now is None else monotonic_now

        def age_ms(packet: FramePacket) -> float:
            if packet.captured_monotonic is not None:
                return max(0.0, (current_monotonic - packet.captured_monotonic) * 1000.0)
            return max(0.0, (current_time - packet.captured_at) * 1000.0)

        def ranking(packet: FramePacket) -> tuple[float, float, int]:
            age = age_ms(packet) / 1000.0
            freshness = 1.0 / (1.0 + age)
            score = (
                self.score_fn(packet.frame)
                if self.score_fn is not None
                else _default_score(packet.frame, self.preview_long_edge)
            )
            return (float(score) + freshness * 0.01, freshness, packet.frame_id)

        fresh_packets = [packet for packet in packets if age_ms(packet) <= self.max_frame_age_ms]
        ranked = sorted(fresh_packets, key=ranking, reverse=True)
        return ranked[: self.top_k]

    def select_from_buffer(
        self,
        buffer: FrameBuffer,
        *,
        now: Optional[float] = None,
        monotonic_now: Optional[float] = None,
    ) -> list[FramePacket]:
        return self.select(
            buffer.snapshot(now=now, monotonic_now=monotonic_now),
            now=now,
            monotonic_now=monotonic_now,
        )
