from label_inspection.extraction.fields import FieldExtractor
from label_inspection.schemas import OCRLine


def test_field_extractor_keeps_line_evidence_and_confidence():
    fields = FieldExtractor().extract(
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
    FieldExtractor().extract([line], source="ppocr")
    assert line.text == "SKU: ABC 123"


def test_field_extractor_reports_parse_miss_separately_from_ocr_lines():
    fields = FieldExtractor().extract([OCRLine("ABC123", 0.99)])
    assert fields["sku"].value is None
    assert fields["sku"].reason == "NOT_FOUND"
