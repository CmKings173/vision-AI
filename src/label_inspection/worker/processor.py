"""Worker-owned OCR, barcode, extraction, and validation stages."""

from __future__ import annotations

import time

from ..barcode.base import BarcodeDecoder, NullBarcodeDecoder
from ..extraction.fields import FieldExtractor
from ..ocr.base import OCRProvider
from ..pipeline.types import PreparedInspection
from ..schemas import (
    BarcodeResult,
    InspectionResult,
    RawOCRResult,
)
from ..timing import new_timing, timed
from ..validation.rules import LabelValidator


class InspectionProcessor:
    """Analyze an exact station-prepared crop without camera access."""

    def __init__(
        self,
        *,
        ocr: OCRProvider,
        barcode: BarcodeDecoder | None = None,
        extractor: FieldExtractor | None = None,
        validator: LabelValidator | None = None,
    ) -> None:
        self.ocr = ocr
        self.barcode = barcode or NullBarcodeDecoder()
        self.extractor = extractor or FieldExtractor()
        self.validator = validator or LabelValidator()

    def process(self, prepared: PreparedInspection) -> InspectionResult:
        started = time.perf_counter()
        timing = new_timing()
        timing.update(prepared.timing)

        with timed(timing, "ocr_ms"):
            try:
                raw_ocr = self.ocr.recognize(prepared.label_crop)
            except Exception:  # noqa: BLE001 - OCR runtime plugin boundary
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
                decoded_barcodes = self.barcode.decode(prepared.label_crop)
                barcode_result = _choose_barcode(decoded_barcodes)
            except Exception:  # noqa: BLE001 - barcode runtime plugin boundary
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
                prepared.quality,
                raw_ocr,
            )

        timing["total_ms"] = (time.perf_counter() - started) * 1000.0
        return InspectionResult(
            event_id=prepared.event_id,
            camera_id=prepared.camera_id,
            frame_id=prepared.frame_id,
            frame_timestamp=prepared.received_at_ms / 1000.0,
            label=prepared.label,
            crop_bbox=prepared.crop_bbox,
            candidate_score=prepared.candidate_score,
            raw_ocr=raw_ocr,
            extracted=extracted,
            barcode=barcode_result,
            barcodes=decoded_barcodes,
            quality=prepared.quality,
            validation=validation,
            timing=timing,
            error=None,
        )


def _choose_barcode(results: list[BarcodeResult]) -> BarcodeResult:
    if not results:
        return BarcodeResult(value=None)
    return max(
        results,
        key=lambda item: (bool(item.value), bool(item.valid), item.confidence or 0.0),
    )
