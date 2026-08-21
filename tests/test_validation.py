from label_inspection.schemas import BarcodeResult, ExtractedField, QualityReport, RawOCRResult
from label_inspection.validation.rules import LabelValidator


def raw(success=True):
    return RawOCRResult(engine="ppocr", success=success)


def test_readable_label_passes_without_required_barcode():
    result = LabelValidator().validate(
        {"sku": ExtractedField("ABC123", 0.95)},
        BarcodeResult(value=None),
        QualityReport(status="PASS"),
        raw(),
    )
    assert result.status == "PASS"


def test_missing_or_low_confidence_field_is_review():
    result = LabelValidator().validate(
        {"sku": ExtractedField(None, 0.0, reason="NOT_FOUND")},
        BarcodeResult(value=None),
        QualityReport(status="PASS"),
        raw(),
    )
    assert result.status == "REVIEW"
    assert "MISSING_SKU" in result.reasons


def test_invalid_field_and_barcode_fail():
    result = LabelValidator(field_patterns={"sku": r"[A-Z]{3}[0-9]{3}"}).validate(
        {"sku": ExtractedField("bad", 0.95)},
        BarcodeResult(value="x", valid=False),
        QualityReport(status="PASS"),
        raw(),
    )
    assert result.status == "FAIL"
    assert "INVALID_SKU_FORMAT" in result.reasons
    assert "BARCODE_INVALID" in result.reasons


def test_ocr_runtime_error_is_error_status():
    result = LabelValidator().validate(
        {},
        BarcodeResult(value=None),
        QualityReport(status="PASS"),
        raw(success=False),
    )
    assert result.status == "ERROR"
    assert result.reasons == ("OCR_ERROR",)
