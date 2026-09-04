from label_inspection.contracts import APPROVED_FOR_AUTOMATED_PASS, ProfileBinding
from label_inspection.extraction.evidence import collect_evidence
from label_inspection.extraction.fields import FieldExtractor
from label_inspection.schemas import BarcodeResult, OCRLine, RawOCRResult


def _approved_extractor(fields):
    return FieldExtractor(
        fields=fields,
        profile_binding=ProfileBinding(
            name="test-profile",
            version="1.0",
            approval_status=APPROVED_FOR_AUTOMATED_PASS,
        ),
    )


def test_field_extractor_keeps_line_evidence_and_confidence():
    fields = _approved_extractor(("sku", "lot")).extract(
        [
            OCRLine("SKU: ABC123", 0.91),
            OCRLine("LOT NO: L-42", 0.88),
        ]
    )
    assert fields["sku"].value == "ABC123"
    assert fields["sku"].source == "ocr"
    assert fields["sku"].line_text == "SKU: ABC123"
    assert fields["lot"].value == "L-42"


def test_field_extractor_preserves_raw_ocr_text():
    line = OCRLine("SKU: ABC 123", 0.91)
    _approved_extractor(("sku", "lot")).extract([line], source="ppocr")
    assert line.text == "SKU: ABC 123"


def test_field_extractor_reports_parse_miss_separately_from_ocr_lines():
    fields = _approved_extractor(("sku", "lot")).extract([OCRLine("ABC123", 0.99)])
    assert fields["sku"].value is None
    assert fields["sku"].reason == "NOT_FOUND"


def test_profile_free_extractor_does_not_invent_a_closed_set_of_fields():
    fields = FieldExtractor.unprofiled().extract(
        [OCRLine("CUSTOMER LABEL FIELD: arbitrary-value", 0.99)]
    )

    assert fields == {}


def test_generic_evidence_preserves_ocr_and_barcode_without_semantics():
    evidence = collect_evidence(
        RawOCRResult(
            engine="ppocr-v6",
            lines=[OCRLine("Customer label field: arbitrary-value", 0.91)],
        ),
        [BarcodeResult(value="ABC-123", format="DataMatrix", valid=True)],
    )

    assert [item.kind for item in evidence] == ["OCR_LINE", "BARCODE"]
    assert evidence[0].text == "Customer label field: arbitrary-value"
    assert evidence[0].confidence == 0.91
    assert evidence[1].text == "ABC-123"
    assert evidence[1].metadata["format"] == "DataMatrix"
