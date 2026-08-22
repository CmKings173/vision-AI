"""Vertical slice: frame selection → label crop → OCR/barcode → JSON result."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from ..barcode.base import BarcodeDecoder, NullBarcodeDecoder
from ..camera.selector import FrameSelector
from ..detection.base import LabelDetector
from ..extraction.fields import FieldExtractor
from ..ocr.base import OCRProvider
from ..preprocessing.crop import CropResult, crop_image
from ..detection.fixed_roi import frame_size
from ..preprocessing.quality import QualityChecker
from ..preprocessing.rectify import rectify_image
from ..schemas import (
    BarcodeResult,
    FramePacket,
    InspectionResult,
    LabelCandidate,
    LabelCandidateScore,
    QualityReport,
    RawOCRResult,
    STAGE_FAILED,
    STAGE_NOT_RUN,
    ValidationResult,
)
from ..timing import new_timing, timed
from ..validation.rules import LabelValidator
from .ranking import CandidateScorer


@dataclass(frozen=True)
class PreparedCandidate:
    packet: FramePacket
    candidate: LabelCandidate
    crop: CropResult
    image: object
    quality: QualityReport
    score: LabelCandidateScore


class InspectionPipeline:
    """Run all local V1 stages in one worker/process.

    ``top_k`` is a detection/fallback budget.  OCR runs once by default on the
    selected crop, so a three-frame buffer does not imply three OCR calls.
    """

    def __init__(
        self,
        *,
        detector: LabelDetector,
        ocr: OCRProvider,
        barcode: Optional[BarcodeDecoder] = None,
        extractor: Optional[FieldExtractor] = None,
        validator: Optional[LabelValidator] = None,
        selector: Optional[FrameSelector] = None,
        quality_checker: Optional[QualityChecker] = None,
        candidate_scorer: Optional[CandidateScorer] = None,
        camera_id: str = "PHONE-01",
        bbox_padding_ratio: float = 0.05,
    ) -> None:
        self.detector = detector
        self.ocr = ocr
        self.barcode = barcode or NullBarcodeDecoder()
        self.extractor = extractor or FieldExtractor()
        self.validator = validator or LabelValidator()
        self.selector = selector or FrameSelector(top_k=3)
        self.quality_checker = quality_checker or QualityChecker()
        self.candidate_scorer = candidate_scorer or CandidateScorer()
        self.camera_id = camera_id
        self.bbox_padding_ratio = bbox_padding_ratio

    def inspect_frame(
        self,
        frame: object,
        *,
        event_id: Optional[str] = None,
        camera_id: Optional[str] = None,
        frame_id: int = 0,
        captured_at: Optional[float] = None,
    ) -> InspectionResult:
        packet = FramePacket(
            frame_id=frame_id,
            captured_at=time.time() if captured_at is None else captured_at,
            frame=frame,
            source="image",
            captured_monotonic=time.monotonic(),
        )
        return self.inspect_packets([packet], event_id=event_id, camera_id=camera_id)

    def inspect_packets(
        self,
        packets: Iterable[FramePacket],
        *,
        event_id: Optional[str] = None,
        camera_id: Optional[str] = None,
    ) -> InspectionResult:
        started = time.perf_counter()
        event_id = event_id or f"INS-{uuid.uuid4().hex[:12].upper()}"
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
            reason = "NO_FRAME" if not packet_list else "STALE_FRAMES"
            timing["total_ms"] = _elapsed_ms(started)
            return InspectionResult(
                event_id=event_id,
                camera_id=camera_id,
                raw_ocr=_not_run_ocr(self.ocr),
                validation=ValidationResult(status="ERROR", reasons=(reason,)),
                timing=timing,
                error=reason,
            )

        prepared: list[PreparedCandidate] = []
        detection_failed = False
        crops_failed = False
        for packet in selected:
            with timed(timing, "detection_ms"):
                try:
                    candidates = self.detector.detect(packet.frame, frame_id=packet.frame_id)
                except Exception:  # adapter boundary: never leak source/model paths
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
                        candidate_image = crop.image
                        corners = _local_corners(candidate, crop)
                        candidate_image, _, _ = rectify_image(candidate_image, corners)
                    with timed(timing, "quality_ms"):
                        quality = self.quality_checker.check(candidate_image)
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
                    prepared.append(
                        PreparedCandidate(
                            packet=packet,
                            candidate=candidate,
                            crop=crop,
                            image=candidate_image,
                            quality=quality,
                            score=score,
                        )
                    )
                except Exception:
                    crops_failed = True

        if not prepared:
            if detection_failed:
                reason, status = "DETECTION_RUNTIME_ERROR", "ERROR"
            elif crops_failed:
                reason, status = "CROP_PREPARATION_ERROR", "ERROR"
            else:
                reason, status = "LABEL_NOT_DETECTED", "REVIEW"
            timing["total_ms"] = _elapsed_ms(started)
            return InspectionResult(
                event_id=event_id,
                camera_id=camera_id,
                raw_ocr=_not_run_ocr(self.ocr),
                validation=ValidationResult(status=status, reasons=(reason,)),
                timing=timing,
                error=reason,
            )

        usable = [item for item in prepared if item.quality.status == "PASS"]
        if not usable:
            best = max(prepared, key=lambda item: (item.score.total, item.candidate.confidence))
            quality_failed = best.quality.state == STAGE_FAILED
            reason = "QUALITY_RUNTIME_ERROR" if quality_failed else "QUALITY_REJECTED"
            status = "ERROR" if quality_failed else "REVIEW"
            quality_reasons = tuple(f"QUALITY_{item}" for item in best.quality.reasons)
            timing["total_ms"] = _elapsed_ms(started)
            return InspectionResult(
                event_id=event_id,
                camera_id=camera_id,
                frame_id=best.packet.frame_id,
                frame_timestamp=best.packet.captured_at,
                label=best.candidate,
                crop_bbox=best.crop.bbox,
                candidate_score=best.score,
                raw_ocr=_not_run_ocr(self.ocr),
                quality=best.quality,
                validation=ValidationResult(
                    status=status,
                    reasons=(reason, *quality_reasons),
                ),
                timing=timing,
                error=reason,
            )

        best = max(usable, key=lambda item: (item.score.total, item.candidate.confidence))

        with timed(timing, "ocr_ms"):
            try:
                raw_ocr = self.ocr.recognize(best.image)
            except Exception:
                raw_ocr = RawOCRResult(
                    engine=getattr(self.ocr, "engine", "ppocr"),
                    success=False,
                    error="OCR_RUNTIME_ERROR",
                    error_code="OCR_RUNTIME_ERROR",
                    error_message="OCR inference failed.",
                )

        decoded_barcodes: list[BarcodeResult] = []
        with timed(timing, "barcode_ms"):
            try:
                decoded_barcodes = self.barcode.decode(best.image)
                barcode_result = _choose_barcode(decoded_barcodes)
            except Exception:
                barcode_result = BarcodeResult(
                    value=None,
                    success=False,
                    error="BARCODE_RUNTIME_ERROR",
                    error_code="BARCODE_RUNTIME_ERROR",
                    error_message="Barcode decoding failed.",
                )
                decoded_barcodes = [barcode_result]

        with timed(timing, "field_extraction_ms"):
            extracted = self.extractor.extract(raw_ocr.lines, source=raw_ocr.engine)

        with timed(timing, "validation_ms"):
            validation = self.validator.validate(
                extracted,
                barcode_result,
                best.quality,
                raw_ocr,
            )

        timing["total_ms"] = _elapsed_ms(started)
        return InspectionResult(
            event_id=event_id,
            camera_id=camera_id,
            frame_id=best.packet.frame_id,
            frame_timestamp=best.packet.captured_at,
            label=best.candidate,
            crop_bbox=best.crop.bbox,
            candidate_score=best.score,
            raw_ocr=raw_ocr,
            extracted=extracted,
            barcode=barcode_result,
            barcodes=decoded_barcodes,
            quality=best.quality,
            validation=validation,
            timing=timing,
            error=None,
        )


def _local_corners(candidate: LabelCandidate, crop: CropResult):
    if not candidate.corners:
        return None
    x1, y1, _, _ = crop.bbox
    return [(x - x1, y - y1) for x, y in candidate.corners]


def _choose_barcode(results: list[BarcodeResult]) -> BarcodeResult:
    if not results:
        return BarcodeResult(value=None)
    return max(
        results,
        key=lambda item: (bool(item.value), bool(item.valid), item.confidence or 0.0),
    )


def _not_run_ocr(ocr: OCRProvider) -> RawOCRResult:
    return RawOCRResult(
        engine=getattr(ocr, "engine", "none"),
        success=False,
        state=STAGE_NOT_RUN,
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
