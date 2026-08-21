"""Detector protocol shared by FixedROI, contour, and YOLO adapters."""

from __future__ import annotations

from typing import Protocol

from ..schemas import LabelCandidate


class LabelDetector(Protocol):
    def detect(self, frame: object, *, frame_id: int | None = None) -> list[LabelCandidate]:
        """Return zero or more label candidates in image coordinates."""
