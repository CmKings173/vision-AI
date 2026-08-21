"""Deterministic detector for a controlled camera and fixed label area."""

from __future__ import annotations

import math
from typing import Optional

from ..schemas import LabelCandidate


def frame_size(frame: object) -> tuple[int, int]:
    shape = getattr(frame, "shape", None)
    if shape is not None and len(shape) >= 2:
        return int(shape[1]), int(shape[0])
    try:
        height = len(frame)  # type: ignore[arg-type]
        width = len(frame[0]) if height else 0  # type: ignore[index]
        return width, height
    except (TypeError, IndexError):
        return 0, 0


def clamp_bbox(
    bbox: tuple[float, float, float, float], width: int, height: int
) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    return (
        max(0.0, min(float(width), x1)),
        max(0.0, min(float(height), y1)),
        max(0.0, min(float(width), x2)),
        max(0.0, min(float(height), y2)),
    )


class FixedROIDetector:
    """Return one explicit ``x1,y1,x2,y2`` ROI for a controlled camera."""

    name = "FixedROI"
    support_level = "SUPPORTED"

    def __init__(
        self,
        roi: Optional[tuple[float, float, float, float]],
        *,
        normalized: bool = True,
        confidence: float = 1.0,
    ) -> None:
        if roi is None:
            raise ValueError("FixedROI detector requires VISION_LABEL_ROI=x1,y1,x2,y2")
        self._validate_roi(roi)
        self.roi = roi
        self.normalized = normalized
        self.confidence = confidence

    @staticmethod
    def parse_roi(value: Optional[str]) -> tuple[float, float, float, float]:
        if not value or not value.strip():
            raise ValueError("FixedROI detector requires VISION_LABEL_ROI=x1,y1,x2,y2")
        try:
            parts = [float(part.strip()) for part in value.split(",")]
        except ValueError as exc:
            raise ValueError("FixedROI detector requires VISION_LABEL_ROI=x1,y1,x2,y2") from exc
        if len(parts) != 4:
            raise ValueError("FixedROI detector requires VISION_LABEL_ROI=x1,y1,x2,y2")
        roi = tuple(parts)  # type: ignore[assignment]
        FixedROIDetector._validate_roi(roi)
        return roi

    @staticmethod
    def _validate_roi(roi: tuple[float, float, float, float]) -> None:
        x1, y1, x2, y2 = roi
        if not all(math.isfinite(value) for value in roi):
            raise ValueError("FixedROI coordinates must be finite numbers")
        if x1 < 0 or y1 < 0:
            raise ValueError("FixedROI x1 and y1 must be >= 0")
        if x2 <= x1 or y2 <= y1:
            raise ValueError("FixedROI requires x2 > x1 and y2 > y1")

    def detect(self, frame: object, *, frame_id: int | None = None) -> list[LabelCandidate]:
        width, height = frame_size(frame)
        if width <= 0 or height <= 0:
            return []

        x1, y1, x2, y2 = self.roi
        if self.normalized:
            x1, x2 = x1 * width, x2 * width
            y1, y2 = y1 * height, y2 * height
        bbox = clamp_bbox((x1, y1, x2, y2), width, height)

        x1, y1, x2, y2 = bbox
        if x2 <= x1 or y2 <= y1:
            raise ValueError("FixedROI is outside the current frame after clamping")
        return [
            LabelCandidate(
                bbox=bbox,
                confidence=self.confidence,
                detector=self.name,
                frame_id=frame_id,
            )
        ]
