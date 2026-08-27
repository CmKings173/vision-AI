"""In-memory boundary types shared by station preparation and worker logic."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from ..schemas import (
    InspectionResult,
    LabelCandidate,
    LabelCandidateScore,
    QualityReport,
)


@dataclass(frozen=True)
class PreparedInspection:
    """Exact prepared pixels and provenance for one inference operation.

    This is an in-memory compatibility boundary, not a serialized job. The
    distributed `InspectionJob` contains artifact references rather than these
    image objects.
    """

    event_id: str
    trigger_id: str
    station_id: str
    camera_id: str
    triggered_at_ms: int
    received_at_ms: int
    prepared_at_ms: int
    selected_frame: object
    label_crop: object
    frame_id: int
    label: LabelCandidate
    crop_bbox: tuple[float, float, float, float]
    candidate_score: LabelCandidateScore
    quality: QualityReport
    timing: Mapping[str, float]
    source_timestamp_ms: int | None = None
    orientation_degrees: int = 0

    def __post_init__(self) -> None:
        if self.orientation_degrees not in {0, 90, 180, 270}:
            raise ValueError("orientation_degrees must be a quarter turn")
        object.__setattr__(self, "timing", MappingProxyType(dict(self.timing)))


@dataclass(frozen=True)
class InspectionExecution:
    """Non-serialized local execution context for exact debug evidence."""

    result: InspectionResult
    prepared: PreparedInspection | None
    label_crop_snapshot: object | None


def snapshot_image(image: object) -> object:
    """Copy image pixels before inference without imposing a NumPy dependency."""

    copy_method = getattr(image, "copy", None)
    if callable(copy_method):
        return copy_method()
    return deepcopy(image)
