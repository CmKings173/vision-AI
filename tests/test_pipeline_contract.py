import json
import time
import pytest

from label_inspection.camera.selector import FrameSelector
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.pipeline.inspection import InspectionPipeline
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import (
    STAGE_FAILED,
    STAGE_NOT_RUN,
    STAGE_SUCCESS,
    BarcodeResult,
    FramePacket,
    OCRLine,
    RawOCRResult,
)
from label_inspection.timing import TIMING_KEYS
from tests.fixtures.quality import sharp_label

pytestmark = pytest.mark.integration


class FakeOCR:
    engine = "fake-ppocr"

    def __init__(self):
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        return RawOCRResult(
            engine="fake-ppocr",
            lines=[OCRLine("SKU: ABC123", 0.95), OCRLine("LOT: L42", 0.9)],
        )


class FakeBarcode:
    def __init__(self):
        self.calls = 0

    def decode(self, image):
        self.calls += 1
        return [BarcodeResult(value="012345", format="CODE128", valid=True)]


def test_vertical_slice_runs_once_for_selected_crop_and_emits_json():
    ocr = FakeOCR()
    barcode = FakeBarcode()
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=ocr,
        barcode=barcode,
        selector=FrameSelector(top_k=3, score_fn=lambda frame: 1.0),
        quality_checker=QualityChecker(min_width=10, min_height=10, min_sharpness=10),
        camera_id="PHONE-01",
    )

    result = pipeline.inspect_frame(sharp_label(), event_id="INS-001")
    payload = result.to_dict()
    json.dumps(payload)

    assert result.validation.status == "PASS"
    assert payload["raw_ocr"]["lines"][0]["text"] == "SKU: ABC123"
    assert payload["extracted"]["sku"]["value"] == "ABC123"
    assert payload["barcode"]["value"] == "012345"
    assert payload["candidate_score"]["crop_sharpness"] > 0
    assert payload["quality"]["brightness_mean"] is not None
    assert "underexposed_ratio" in payload["quality"]
    assert set(TIMING_KEYS).issubset(payload["timing"])
    assert ocr.calls == 1
    assert barcode.calls == 1


class RaisingOCR:
    engine = "ppocr"

    def recognize(self, image):
        raise RuntimeError("/private/model/path should not leak")


class RaisingBarcode:
    def decode(self, image):
        raise RuntimeError("decoder exploded")


def test_ocr_and_barcode_exceptions_return_structured_failure_with_timing():
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=RaisingOCR(),
        barcode=RaisingBarcode(),
        selector=FrameSelector(top_k=1),
        quality_checker=QualityChecker(min_sharpness=10),
    )

    result = pipeline.inspect_frame(sharp_label(), event_id="INS-ERR")
    payload = result.to_dict()
    json.dumps(payload)

    assert result.validation.status == "ERROR"
    assert result.raw_ocr.error_code == "OCR_RUNTIME_ERROR"
    assert result.barcode.error_code == "BARCODE_RUNTIME_ERROR"
    assert result.raw_ocr.state == STAGE_FAILED
    assert result.barcode.state == STAGE_FAILED
    assert result.quality.state == STAGE_SUCCESS
    assert "/private/model/path" not in json.dumps(payload)
    assert set(TIMING_KEYS).issubset(result.timing)


def test_ocr_failure_does_not_prevent_successful_barcode_result():
    barcode = FakeBarcode()
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=RaisingOCR(),
        barcode=barcode,
        selector=FrameSelector(top_k=1),
        quality_checker=QualityChecker(min_sharpness=10),
    )

    result = pipeline.inspect_frame(sharp_label())

    assert result.validation.status == "ERROR"
    assert result.raw_ocr.error_code == "OCR_RUNTIME_ERROR"
    assert result.barcode.value == "012345"
    assert barcode.calls == 1


def test_barcode_failure_cannot_become_pass_when_ocr_succeeds():
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=FakeOCR(),
        barcode=RaisingBarcode(),
        selector=FrameSelector(top_k=1),
        quality_checker=QualityChecker(min_sharpness=10),
    )

    result = pipeline.inspect_frame(sharp_label())

    assert result.validation.status == "REVIEW"
    assert "BARCODE_RUNTIME_ERROR" in result.validation.reasons


class LowConfidenceOCR:
    engine = "ppocr"

    def recognize(self, image):
        return RawOCRResult(
            engine=self.engine,
            lines=[OCRLine("SKU: LOW123", 0.42)],
        )


def test_low_confidence_line_stays_raw_but_business_field_is_reviewed():
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=LowConfidenceOCR(),
        selector=FrameSelector(top_k=1),
        quality_checker=QualityChecker(min_sharpness=10),
    )

    result = pipeline.inspect_frame(sharp_label())

    assert result.raw_ocr.lines[0].text == "SKU: LOW123"
    assert result.extracted["sku"].value == "LOW123"
    assert result.extracted["sku"].source == "ppocr"
    assert result.validation.status == "REVIEW"
    assert "LOW_CONFIDENCE_SKU" in result.validation.reasons


def test_no_frame_failure_still_contains_every_timing_key():
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=FakeOCR(),
        selector=FrameSelector(top_k=1),
    )

    result = pipeline.inspect_packets([])

    assert result.validation.status == "ERROR"
    assert result.validation.reasons == ("NO_FRAME",)
    assert result.raw_ocr.state == STAGE_NOT_RUN
    assert result.barcode.state == STAGE_NOT_RUN
    assert result.quality.state == STAGE_NOT_RUN
    assert result.barcode.success is False
    assert result.quality.to_dict()["passed"] is None
    assert set(TIMING_KEYS).issubset(result.timing)


def test_stale_frame_is_rejected_before_detection_and_ocr():
    ocr = FakeOCR()
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=ocr,
        selector=FrameSelector(top_k=1, max_frame_age_ms=100),
    )
    stale = FramePacket(
        1,
        time.time() - 10,
        sharp_label(),
        captured_monotonic=time.monotonic() - 10,
    )

    result = pipeline.inspect_packets([stale])

    assert result.validation.reasons == ("STALE_FRAMES",)
    assert ocr.calls == 0


class NoLabelDetector:
    def detect(self, frame, *, frame_id=None):
        return []


def test_no_label_leaves_downstream_stages_not_run():
    ocr = FakeOCR()
    barcode = FakeBarcode()
    pipeline = InspectionPipeline(
        detector=NoLabelDetector(),
        ocr=ocr,
        barcode=barcode,
        selector=FrameSelector(top_k=1),
    )

    result = pipeline.inspect_frame(sharp_label())

    assert result.validation.reasons == ("LABEL_NOT_DETECTED",)
    assert result.raw_ocr.state == STAGE_NOT_RUN
    assert result.barcode.state == STAGE_NOT_RUN
    assert result.quality.state == STAGE_NOT_RUN
    assert ocr.calls == 0
    assert barcode.calls == 0


def test_quality_rejection_skips_ocr_and_barcode():
    ocr = FakeOCR()
    barcode = FakeBarcode()
    pipeline = InspectionPipeline(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        ocr=ocr,
        barcode=barcode,
        selector=FrameSelector(top_k=1),
        quality_checker=QualityChecker(min_sharpness=1_000_000_000),
    )

    result = pipeline.inspect_frame(sharp_label())

    assert result.validation.status == "REVIEW"
    assert result.validation.reasons[0] == "QUALITY_REJECTED"
    assert result.quality.state == STAGE_SUCCESS
    assert result.quality.to_dict()["passed"] is False
    assert result.raw_ocr.state == STAGE_NOT_RUN
    assert result.barcode.state == STAGE_NOT_RUN
    assert ocr.calls == 0
    assert barcode.calls == 0
