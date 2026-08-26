"""Station-owned frame selection, orientation, crop, and quality gate."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from ..camera.selector import FrameSelector
from ..contracts import (
    BusinessStatus,
    InspectionError,
    ProcessingStatus,
    TriggerEvent,
    epoch_ms_now,
)
from ..contracts import (
    InspectionResult as ContractInspectionResult,
)
from ..detection.base import LabelDetector
from ..detection.fixed_roi import frame_size
from ..pipeline.ranking import CandidateScorer
from ..pipeline.types import PreparedInspection
from ..preprocessing.crop import CropResult, crop_image
from ..preprocessing.orientation import normalize_orientation
from ..preprocessing.quality import QualityChecker
from ..preprocessing.rectify import rectify_image
from ..schemas import (
    STAGE_FAILED,
    FramePacket,
    LabelCandidate,
    LabelCandidateScore,
    QualityReport,
)
from ..timing import new_timing, timed


@dataclass(frozen=True)
class _PreparedCandidate:
    packet: FramePacket
    selected_frame: object
    candidate: LabelCandidate
    crop: CropResult
    label_crop: object
    quality: QualityReport
    score: LabelCandidateScore


@dataclass(frozen=True)
class PreparationOutcome:
    event_id: str
    trigger_id: str
    station_id: str
    camera_id: str
    triggered_at_ms: int
    processing_status: ProcessingStatus
    business_status: BusinessStatus | None
    inference_required: bool
    reasons: tuple[str, ...]
    timing: Mapping[str, float]
    completed_at_ms: int
    prepared: PreparedInspection | None = None
    error: InspectionError | None = None
    legacy_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timing", MappingProxyType(dict(self.timing)))

    @classmethod
    def internal_error(
        cls,
        *,
        trigger: TriggerEvent,
        elapsed_ms: float,
    ) -> PreparationOutcome:
        """Normalize an unexpected controller-boundary preparation failure."""

        return cls(
            event_id=trigger.event_id,
            trigger_id=trigger.trigger_id,
            station_id=trigger.station_id,
            camera_id=trigger.camera_id,
            triggered_at_ms=trigger.triggered_at_ms,
            processing_status=ProcessingStatus.ERROR,
            business_status=None,
            inference_required=False,
            reasons=("INTERNAL_ERROR",),
            timing={"total_ms": max(0.0, float(elapsed_ms))},
            completed_at_ms=max(epoch_ms_now(), trigger.triggered_at_ms),
            error=InspectionError(
                code="INTERNAL_ERROR",
                stage="PREPARATION",
                message="Unexpected station preparation failure.",
                retryable=True,
                attempt=0,
            ),
            legacy_reason="INTERNAL_ERROR",
        )

    def to_terminal_result(self) -> ContractInspectionResult:
        if self.inference_required or self.processing_status is ProcessingStatus.PREPARED:
            raise ValueError("prepared inference outcome is not a terminal station result")
        if self.processing_status is ProcessingStatus.COMPLETED:
            quality = {} if self.prepared is None else self.prepared.quality.to_dict()
            return ContractInspectionResult.quality_rejected(
                event_id=self.event_id,
                trigger_id=self.trigger_id,
                station_id=self.station_id,
                camera_id=self.camera_id,
                created_at_ms=self.triggered_at_ms,
                completed_at_ms=self.completed_at_ms,
                quality=quality,
                reasons=self.reasons,
            )
        if self.error is None:
            raise ValueError("technical terminal outcome requires an error")
        return ContractInspectionResult.preparation_error(
            event_id=self.event_id,
            trigger_id=self.trigger_id,
            station_id=self.station_id,
            camera_id=self.camera_id,
            created_at_ms=self.triggered_at_ms,
            completed_at_ms=self.completed_at_ms,
            error=self.error,
            reasons=self.reasons,
        )


class StationPreparer:
    """Choose and freeze the exact image a worker must analyze."""

    def __init__(
        self,
        *,
        detector: LabelDetector,
        selector: FrameSelector | None = None,
        quality_checker: QualityChecker | None = None,
        candidate_scorer: CandidateScorer | None = None,
        station_id: str = "STATION-01",
        camera_id: str = "PHONE-01",
        rotate_degrees: int = 0,
        bbox_padding_ratio: float = 0.05,
    ) -> None:
        if rotate_degrees not in {0, 90, 180, 270}:
            raise ValueError("rotate_degrees must be one of 0, 90, 180, or 270")
        self.detector = detector
        self.selector = selector or FrameSelector(top_k=3)
        self.quality_checker = quality_checker or QualityChecker()
        self.candidate_scorer = candidate_scorer or CandidateScorer()
        self.station_id = station_id
        self.camera_id = camera_id
        self.rotate_degrees = rotate_degrees
        self.bbox_padding_ratio = bbox_padding_ratio

    def prepare_trigger(
        self,
        packets: Iterable[FramePacket],
        *,
        trigger: TriggerEvent,
        quality_observation: bool = False,
    ) -> PreparationOutcome:
        return self.prepare_packets(
            packets,
            event_id=trigger.event_id,
            trigger_id=trigger.trigger_id,
            station_id=trigger.station_id,
            camera_id=trigger.camera_id,
            triggered_at_ms=trigger.triggered_at_ms,
            quality_observation=quality_observation,
        )

    def prepare_packets(
        self,
        packets: Iterable[FramePacket],
        *,
        event_id: str,
        trigger_id: str,
        triggered_at_ms: int,
        station_id: str | None = None,
        camera_id: str | None = None,
        quality_observation: bool = False,
    ) -> PreparationOutcome:
        started = time.perf_counter()
        station_id = station_id or self.station_id
        camera_id = camera_id or self.camera_id
        packet_list = list(packets)
        timing = new_timing()
        now_wall = time.time()
        now_monotonic = time.monotonic()
        with timed(timing, "frame_selection_ms"):
            selected = self.selector.select(
                packet_list,
                now=now_wall,
                monotonic_now=now_monotonic,
            )

        if not selected:
            if packet_list:
                code, legacy_reason = "CAMERA_STALE_FRAME", "STALE_FRAMES"
                message = "No fresh buffered frame is available."
            else:
                code, legacy_reason = "NO_FRAME_CANDIDATE", "NO_FRAME"
                message = "No buffered frame candidate is available."
            return self._technical_outcome(
                event_id=event_id,
                trigger_id=trigger_id,
                station_id=station_id,
                camera_id=camera_id,
                triggered_at_ms=triggered_at_ms,
                code=code,
                legacy_reason=legacy_reason,
                message=message,
                timing=timing,
                started=started,
            )

        prepared_candidates: list[_PreparedCandidate] = []
        detection_failed = False
        crops_failed = False
        for source_packet in selected:
            oriented_frame = normalize_orientation(
                source_packet.frame, self.rotate_degrees
            )
            packet = replace(source_packet, frame=oriented_frame)
            with timed(timing, "detection_ms"):
                try:
                    candidates = self.detector.detect(
                        packet.frame, frame_id=packet.frame_id
                    )
                except Exception:  # noqa: BLE001 - detector plugin boundary
                    detection_failed = True
                    continue
            frame_width, frame_height = frame_size(packet.frame)
            for candidate in candidates:
                try:
                    with timed(timing, "crop_rectify_ms"):
                        crop = crop_image(
                            packet.frame,
                            candidate.bbox,
                            padding_ratio=self.bbox_padding_ratio,
                        )
                        label_crop = crop.image
                        corners = _local_corners(candidate, crop)
                        label_crop, _, _ = rectify_image(label_crop, corners)
                        label_crop = _copy_image(label_crop)
                    with timed(timing, "quality_ms"):
                        quality = self.quality_checker.check(label_crop)
                    with timed(timing, "candidate_ranking_ms"):
                        score = self.candidate_scorer.score(
                            packet,
                            candidate,
                            quality,
                            frame_width=frame_width,
                            frame_height=frame_height,
                            now_wall=now_wall,
                            now_monotonic=now_monotonic,
                        )
                    prepared_candidates.append(
                        _PreparedCandidate(
                            packet=packet,
                            selected_frame=_copy_image(packet.frame),
                            candidate=candidate,
                            crop=crop,
                            label_crop=label_crop,
                            quality=quality,
                            score=score,
                        )
                    )
                except Exception:  # noqa: BLE001 - image/CV plugin boundary
                    crops_failed = True

        if not prepared_candidates:
            if detection_failed:
                code, legacy_reason, message = (
                    "DETECTION_RUNTIME_ERROR",
                    "DETECTION_RUNTIME_ERROR",
                    "Label detection failed.",
                )
            elif crops_failed:
                code, legacy_reason, message = (
                    "CROP_FAILED",
                    "CROP_PREPARATION_ERROR",
                    "Label crop preparation failed.",
                )
            else:
                code, legacy_reason, message = (
                    "NO_FRAME_CANDIDATE",
                    "LABEL_NOT_DETECTED",
                    "No label candidate was detected.",
                )
            return self._technical_outcome(
                event_id=event_id,
                trigger_id=trigger_id,
                station_id=station_id,
                camera_id=camera_id,
                triggered_at_ms=triggered_at_ms,
                code=code,
                legacy_reason=legacy_reason,
                message=message,
                timing=timing,
                started=started,
            )

        usable = [item for item in prepared_candidates if item.quality.status == "PASS"]
        if not usable:
            best = max(
                prepared_candidates,
                key=lambda item: (item.score.total, item.candidate.confidence),
            )
            prepared = _to_prepared(
                best,
                event_id=event_id,
                trigger_id=trigger_id,
                station_id=station_id,
                camera_id=camera_id,
                triggered_at_ms=triggered_at_ms,
                orientation_degrees=self.rotate_degrees,
                timing=timing,
            )
            if quality_observation:
                return self._inference_outcome(prepared, timing, started)
            if best.quality.state == STAGE_FAILED:
                return self._technical_outcome(
                    event_id=event_id,
                    trigger_id=trigger_id,
                    station_id=station_id,
                    camera_id=camera_id,
                    triggered_at_ms=triggered_at_ms,
                    code="QUALITY_RUNTIME_ERROR",
                    legacy_reason="QUALITY_RUNTIME_ERROR",
                    message="Quality measurement failed.",
                    timing=timing,
                    started=started,
                    prepared=prepared,
                )
            reasons = (
                "QUALITY_REJECTED",
                *(f"QUALITY_{reason}" for reason in best.quality.reasons),
            )
            timing["total_ms"] = _elapsed_ms(started)
            return PreparationOutcome(
                event_id=event_id,
                trigger_id=trigger_id,
                station_id=station_id,
                camera_id=camera_id,
                triggered_at_ms=triggered_at_ms,
                processing_status=ProcessingStatus.COMPLETED,
                business_status=BusinessStatus.REVIEW,
                inference_required=False,
                reasons=tuple(reasons),
                timing=timing,
                completed_at_ms=epoch_ms_now(),
                prepared=prepared,
                legacy_reason="QUALITY_REJECTED",
            )

        best = max(usable, key=lambda item: (item.score.total, item.candidate.confidence))
        prepared = _to_prepared(
            best,
            event_id=event_id,
            trigger_id=trigger_id,
            station_id=station_id,
            camera_id=camera_id,
            triggered_at_ms=triggered_at_ms,
            orientation_degrees=self.rotate_degrees,
            timing=timing,
        )
        return self._inference_outcome(prepared, timing, started)

    @staticmethod
    def _inference_outcome(
        prepared: PreparedInspection,
        timing: dict[str, float],
        started: float,
    ) -> PreparationOutcome:
        timing["total_ms"] = _elapsed_ms(started)
        return PreparationOutcome(
            event_id=prepared.event_id,
            trigger_id=prepared.trigger_id,
            station_id=prepared.station_id,
            camera_id=prepared.camera_id,
            triggered_at_ms=prepared.triggered_at_ms,
            processing_status=ProcessingStatus.PREPARED,
            business_status=None,
            inference_required=True,
            reasons=(),
            timing=timing,
            completed_at_ms=prepared.prepared_at_ms,
            prepared=prepared,
        )

    @staticmethod
    def _technical_outcome(
        *,
        event_id: str,
        trigger_id: str,
        station_id: str,
        camera_id: str,
        triggered_at_ms: int,
        code: str,
        legacy_reason: str,
        message: str,
        timing: dict[str, float],
        started: float,
        prepared: PreparedInspection | None = None,
    ) -> PreparationOutcome:
        timing["total_ms"] = _elapsed_ms(started)
        return PreparationOutcome(
            event_id=event_id,
            trigger_id=trigger_id,
            station_id=station_id,
            camera_id=camera_id,
            triggered_at_ms=triggered_at_ms,
            processing_status=ProcessingStatus.ERROR,
            business_status=None,
            inference_required=False,
            reasons=(code,),
            timing=timing,
            completed_at_ms=epoch_ms_now(),
            prepared=prepared,
            error=InspectionError(
                code=code,
                stage="PREPARATION",
                message=message,
                retryable=code
                in {"CAMERA_STALE_FRAME", "DETECTION_RUNTIME_ERROR", "QUALITY_RUNTIME_ERROR"},
                attempt=0,
            ),
            legacy_reason=legacy_reason,
        )


def _to_prepared(
    item: _PreparedCandidate,
    *,
    event_id: str,
    trigger_id: str,
    station_id: str,
    camera_id: str,
    triggered_at_ms: int,
    orientation_degrees: int,
    timing: Mapping[str, float],
) -> PreparedInspection:
    return PreparedInspection(
        event_id=event_id,
        trigger_id=trigger_id,
        station_id=station_id,
        camera_id=camera_id,
        triggered_at_ms=triggered_at_ms,
        received_at_ms=max(0, round(item.packet.captured_at * 1000)),
        source_timestamp_ms=None,
        prepared_at_ms=epoch_ms_now(),
        selected_frame=item.selected_frame,
        label_crop=item.label_crop,
        frame_id=item.packet.frame_id,
        label=item.candidate,
        crop_bbox=item.crop.bbox,
        candidate_score=item.score,
        quality=item.quality,
        timing=timing,
        orientation_degrees=orientation_degrees,
    )


def _local_corners(candidate: LabelCandidate, crop: CropResult):
    if not candidate.corners:
        return None
    x1, y1, _, _ = crop.bbox
    return [(x - x1, y - y1) for x, y in candidate.corners]


def _copy_image(image: object) -> object:
    copy_method = getattr(image, "copy", None)
    if callable(copy_method):
        return copy_method()
    return image


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
