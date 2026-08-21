"""Optional contour-based label candidate detector for the controlled V1 setup."""

from __future__ import annotations

from typing import Any

from ..schemas import LabelCandidate
from .fixed_roi import frame_size


class ContourDetector:
    name = "Contour"
    support_level = "EXPERIMENTAL"

    def __init__(
        self,
        *,
        min_area_ratio: float = 0.02,
        max_candidates: int = 5,
        threshold: int = 0,
    ) -> None:
        self.min_area_ratio = min_area_ratio
        self.max_candidates = max_candidates
        self.threshold = threshold

    def detect(self, frame: object, *, frame_id: int | None = None) -> list[LabelCandidate]:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("ContourDetector requires opencv-python-headless") from exc

        width, height = frame_size(frame)
        if width <= 0 or height <= 0:
            return []
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(getattr(frame, "shape", ())) == 3 else frame
        if self.threshold > 0:
            _, binary = cv2.threshold(gray, self.threshold, 255, cv2.THRESH_BINARY)
        else:
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_area = width * height * self.min_area_ratio
        candidates: list[LabelCandidate] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < min_area:
                continue
            x, y, box_width, box_height = cv2.boundingRect(contour)
            corners = None
            approximation = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
            if len(approximation) == 4:
                corners = tuple(
                    (float(point[0][0]), float(point[0][1])) for point in approximation
                )
            candidates.append(
                LabelCandidate(
                    bbox=(float(x), float(y), float(x + box_width), float(y + box_height)),
                    confidence=_geometric_confidence(
                        area,
                        box_width,
                        box_height,
                        is_quadrilateral=corners is not None,
                    ),
                    detector=self.name,
                    frame_id=frame_id,
                    corners=corners,
                )
            )
        candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
        return candidates[: self.max_candidates]


def _geometric_confidence(
    contour_area: float,
    box_width: int,
    box_height: int,
    *,
    is_quadrilateral: bool,
) -> float:
    """Shape-only score; candidate area is counted later by CandidateScorer."""

    box_area = max(1.0, float(box_width * box_height))
    extent = max(0.0, min(1.0, contour_area / box_area))
    quadrilateral = 1.0 if is_quadrilateral else 0.5
    return extent * 0.75 + quadrilateral * 0.25
