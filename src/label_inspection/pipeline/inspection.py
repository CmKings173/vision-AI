"""Local compatibility façade over station preparation and worker processing."""

from __future__ import annotations

import time
import uuid
from typing import Iterable, Optional

from ..barcode.base import BarcodeDecoder
from ..camera.selector import FrameSelector
from ..detection.base import LabelDetector
from ..extraction.fields import FieldExtractor
from ..ocr.base import OCRProvider
from ..pipeline.ranking import CandidateScorer
from ..preprocessing.quality import QualityChecker
from ..schemas import (
    FramePacket,
    InspectionResult,
    QualityReport,
    RawOCRResult,
    STAGE_NOT_RUN,
    ValidationResult,
)
from ..station.preparation import PreparationOutcome, StationPreparer
from ..validation.rules import LabelValidator
from ..worker.processor import InspectionProcessor


class InspectionPipeline:
    """Preserve the existing synchronous POC API through clean boundaries.

    New asynchronous station code uses ``StationPreparer`` directly and never
    constructs this façade. The inference worker uses ``InspectionProcessor``.
    """

    def __init__(
        self,
        *,
        detector: Optional[LabelDetector] = None,
        ocr: Optional[OCRProvider] = None,
        barcode: Optional[BarcodeDecoder] = None,
        extractor: Optional[FieldExtractor] = None,
        validator: Optional[LabelValidator] = None,
        selector: Optional[FrameSelector] = None,
        quality_checker: Optional[QualityChecker] = None,
        candidate_scorer: Optional[CandidateScorer] = None,
        camera_id: Optional[str] = None,
        station_id: str = "STATION-01",
        rotate_degrees: int = 0,
        bbox_padding_ratio: float = 0.05,
        preparer: Optional[StationPreparer] = None,
        processor: Optional[InspectionProcessor] = None,
    ) -> None:
        if preparer is None:
            if detector is None:
                raise ValueError("detector is required when preparer is not provided")
            preparer = StationPreparer(
                detector=detector,
                selector=selector,
                quality_checker=quality_checker,
                candidate_scorer=candidate_scorer,
                station_id=station_id,
                camera_id=camera_id or "PHONE-01",
                rotate_degrees=rotate_degrees,
                bbox_padding_ratio=bbox_padding_ratio,
            )
        if processor is None:
            if ocr is None:
                raise ValueError("ocr is required when processor is not provided")
            processor = InspectionProcessor(
                ocr=ocr,
                barcode=barcode,
                extractor=extractor,
                validator=validator,
            )

        self.preparer = preparer
        self.processor = processor
        self.detector = preparer.detector
        self.selector = preparer.selector
        self.quality_checker = preparer.quality_checker
        self.candidate_scorer = preparer.candidate_scorer
        self.bbox_padding_ratio = preparer.bbox_padding_ratio
        self.ocr = processor.ocr
        self.barcode = processor.barcode
        self.extractor = processor.extractor
        self.validator = processor.validator
        self.camera_id = preparer.camera_id

    def inspect_frame(
        self,
        frame: object,
        *,
        event_id: Optional[str] = None,
        camera_id: Optional[str] = None,
        frame_id: int = 0,
        captured_at: Optional[float] = None,
        quality_observation: bool = False,
    ) -> InspectionResult:
        packet = FramePacket(
            frame_id=frame_id,
            captured_at=time.time() if captured_at is None else captured_at,
            frame=frame,
            source="image",
            captured_monotonic=time.monotonic(),
        )
        return self.inspect_packets(
            [packet],
            event_id=event_id,
            camera_id=camera_id,
            quality_observation=quality_observation,
        )

    def inspect_packets(
        self,
        packets: Iterable[FramePacket],
        *,
        event_id: Optional[str] = None,
        camera_id: Optional[str] = None,
        quality_observation: bool = False,
    ) -> InspectionResult:
        started = time.perf_counter()
        local_event_id = event_id or f"INS-{uuid.uuid4().hex[:12].upper()}"
        outcome = self.preparer.prepare_packets(
            packets,
            event_id=local_event_id,
            trigger_id=str(uuid.uuid4()),
            station_id=self.preparer.station_id,
            camera_id=camera_id or self.camera_id,
            triggered_at_ms=time.time_ns() // 1_000_000,
            quality_observation=quality_observation,
        )

        if outcome.inference_required:
            if outcome.prepared is None:
                raise RuntimeError("inference outcome is missing prepared pixels")
            result = self.processor.process(outcome.prepared)
            result.timing["total_ms"] = _elapsed_ms(started)
            return result

        result = _legacy_terminal_result(outcome, self.ocr)
        result.timing["total_ms"] = _elapsed_ms(started)
        return result


def _legacy_terminal_result(
    outcome: PreparationOutcome,
    ocr: OCRProvider,
) -> InspectionResult:
    prepared = outcome.prepared
    legacy_reason = outcome.legacy_reason or (
        outcome.error.code if outcome.error is not None else "PREPARATION_ERROR"
    )
    if outcome.processing_status.value == "COMPLETED":
        validation_status = "REVIEW"
        validation_reasons = outcome.reasons
    else:
        validation_status = "REVIEW" if legacy_reason == "LABEL_NOT_DETECTED" else "ERROR"
        validation_reasons = (legacy_reason,)

    return InspectionResult(
        event_id=outcome.event_id,
        camera_id=outcome.camera_id,
        frame_id=None if prepared is None else prepared.frame_id,
        frame_timestamp=(
            None if prepared is None else prepared.received_at_ms / 1000.0
        ),
        label=None if prepared is None else prepared.label,
        crop_bbox=None if prepared is None else prepared.crop_bbox,
        candidate_score=None if prepared is None else prepared.candidate_score,
        raw_ocr=_not_run_ocr(ocr),
        quality=(
            QualityReport(status="NOT_RUN", state=STAGE_NOT_RUN)
            if prepared is None
            else prepared.quality
        ),
        validation=ValidationResult(
            status=validation_status,
            reasons=validation_reasons,
        ),
        timing=dict(outcome.timing),
        error=legacy_reason,
    )


def _not_run_ocr(ocr: OCRProvider) -> RawOCRResult:
    return RawOCRResult(
        engine=getattr(ocr, "engine", "none"),
        success=False,
        state=STAGE_NOT_RUN,
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
