from label_inspection.contracts import APPROVED_FOR_AUTOMATED_PASS, ProfileBinding
from label_inspection.extraction.fields import FieldExtractor
from label_inspection.schemas import OCRLine


def test_shopee_tracking_number_and_order_id_are_extracted_with_source_evidence():
    lines = [
        OCRLine("SPXVN067769969098", 0.98),
        OCRLine("26081309AQPBKJM", 0.96),
    ]

    extracted = FieldExtractor(
        fields=("tracking_number", "order_id"),
        profile_binding=ProfileBinding(
            name="test-profile",
            version="1.0",
            approval_status=APPROVED_FOR_AUTOMATED_PASS,
        ),
    ).extract(lines)

    assert extracted["tracking_number"].value == "SPXVN067769969098"
    assert extracted["order_id"].value == "26081309AQPBKJM"
    assert extracted["tracking_number"].line_text == lines[0].text
