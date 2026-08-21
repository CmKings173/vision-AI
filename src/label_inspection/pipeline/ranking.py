"""Centralized scoring for prepared label crops from Top-K frames."""

from __future__ import annotations

from dataclasses import dataclass

from ..schemas import FramePacket, LabelCandidate, LabelCandidateScore, QualityReport


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


@dataclass(frozen=True)
class CandidateScoreWeights:
    detection: float = 0.25
    sharpness: float = 0.35
    exposure: float = 0.15
    area: float = 0.05
    freshness: float = 0.05
    glare: float = 0.05
    validity: float = 0.10

    def normalized(self) -> "CandidateScoreWeights":
        values = (
            self.detection,
            self.sharpness,
            self.exposure,
            self.area,
            self.freshness,
            self.glare,
            self.validity,
        )
        if any(value < 0 for value in values):
            raise ValueError("candidate score weights must be non-negative")
        total = sum(values)
        if total <= 0:
            raise ValueError("candidate score weights must have a positive sum")
        return CandidateScoreWeights(*(value / total for value in values))


class CandidateScorer:
    """Compare candidates using crop metrics; global frame quality is excluded."""

    def __init__(
        self,
        *,
        weights: CandidateScoreWeights | None = None,
        sharpness_reference: float = 500.0,
        max_frame_age_ms: int = 1000,
    ) -> None:
        if sharpness_reference <= 0:
            raise ValueError("sharpness_reference must be > 0")
        if max_frame_age_ms <= 0:
            raise ValueError("max_frame_age_ms must be > 0")
        self.weights = (weights or CandidateScoreWeights()).normalized()
        self.sharpness_reference = sharpness_reference
        self.max_frame_age_ms = max_frame_age_ms

    def score(
        self,
        packet: FramePacket,
        candidate: LabelCandidate,
        quality: QualityReport,
        *,
        frame_width: int,
        frame_height: int,
        now_wall: float,
        now_monotonic: float,
    ) -> LabelCandidateScore:
        detection = _clamp01(candidate.confidence)
        sharpness = _clamp01((quality.sharpness or 0.0) / self.sharpness_reference)
        under = _clamp01(quality.underexposed_ratio or 0.0)
        over = _clamp01(quality.overexposed_ratio or 0.0)
        exposure = _clamp01(1.0 - under - over)
        glare = _clamp01(1.0 - (quality.glare_ratio or 0.0))
        x1, y1, x2, y2 = candidate.bbox
        frame_area = max(1.0, float(frame_width * frame_height))
        area = _clamp01(max(0.0, x2 - x1) * max(0.0, y2 - y1) / frame_area)
        if packet.captured_monotonic is not None:
            age_ms = max(0.0, (now_monotonic - packet.captured_monotonic) * 1000.0)
        else:
            age_ms = max(0.0, (now_wall - packet.captured_at) * 1000.0)
        freshness = _clamp01(1.0 - age_ms / self.max_frame_age_ms)
        invalid_reasons = {"EMPTY_CROP", "LOW_RESOLUTION", "METRICS_UNAVAILABLE"}
        validity = 0.0 if invalid_reasons.intersection(quality.reasons) else 1.0
        w = self.weights
        total = (
            w.detection * detection
            + w.sharpness * sharpness
            + w.exposure * exposure
            + w.area * area
            + w.freshness * freshness
            + w.glare * glare
            + w.validity * validity
        )
        return LabelCandidateScore(
            total=total,
            detection_confidence=detection,
            crop_sharpness=sharpness,
            crop_exposure=exposure,
            crop_glare=glare,
            label_area_ratio=area,
            frame_freshness=freshness,
            crop_validity=validity,
        )
