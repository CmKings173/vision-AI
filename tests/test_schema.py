import json

from label_inspection.schemas import (
    BarcodeResult,
    ExtractedField,
    InspectionResult,
    LabelCandidate,
    OCRLine,
    QualityReport,
    RawOCRResult,
    STAGE_NOT_RUN,
    ValidationResult,
)


def test_inspection_json_keeps_raw_ocr_and_extracted_fields():
    result = InspectionResult(
        event_id="INS-001",
        camera_id="PHONE-01",
        frame_id=7,
        label=LabelCandidate((10, 20, 100, 50), confidence=0.98, detector="FixedROI"),
        raw_ocr=RawOCRResult(
            engine="ppocr",
            lines=[OCRLine(text="SKU: ABC123", confidence=0.91)],
        ),
        extracted={
            "sku": ExtractedField(
                value="ABC123",
                confidence=0.91,
                source="raw_ocr",
                line_text="SKU: ABC123",
            )
        },
        barcode=BarcodeResult(value="012345", format="EAN-13", valid=True),
        quality=QualityReport(status="PASS", width=640, height=480),
        validation=ValidationResult(status="PASS"),
        timing={"ocr_ms": 12.5},
    )

    payload = result.to_dict()
    json.dumps(payload)

    assert payload["raw_ocr"]["lines"][0]["text"] == "SKU: ABC123"
    assert payload["extracted"]["sku"]["value"] == "ABC123"
    assert payload["barcode"]["value"] == "012345"
    assert payload["validation"]["status"] == "PASS"


def test_schema_defaults_make_not_run_result_debuggable():
    result = InspectionResult(event_id="INS-002", camera_id="PHONE-01")

    assert result.raw_ocr.engine == "none"
    assert result.raw_ocr.state == STAGE_NOT_RUN
    assert result.raw_ocr.success is False
    assert result.barcode.state == STAGE_NOT_RUN
    assert result.barcode.success is False
    assert result.quality.state == STAGE_NOT_RUN
    assert result.quality.to_dict()["passed"] is None
    assert result.validation.status == "ERROR"
    assert result.validation.reasons == ("NOT_RUN",)
