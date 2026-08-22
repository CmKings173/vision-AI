from label_inspection.extraction.profiles import (
    DGX_SPARK_LABEL_FIELDS,
    build_extractor,
)
from label_inspection.schemas import OCRLine


def test_dgx_spark_profile_extracts_only_label_business_fields():
    lines = [
        OCRLine("Customer Part Number: 699-12345-0000-000", 0.98),
        OCRLine("S/O NO: SO-20260822-01", 0.97),
        OCRLine("Our Part Number: DGX-SPARK-001", 0.96),
        OCRLine("QTY: 2", 0.95),
        OCRLine("N.W.: 12.5 KG", 0.94),
        OCRLine("G.W.: 15.0 KG", 0.93),
        OCRLine("Carton No: 3/10", 0.92),
        OCRLine("SPXVN067769969098", 0.99),
    ]

    extractor = build_extractor("dgx_spark_label")
    extracted = extractor.extract(lines, source="ppocr_v6")

    assert extractor.fields == DGX_SPARK_LABEL_FIELDS
    assert set(extracted) == set(DGX_SPARK_LABEL_FIELDS)
    assert extracted["customer_part_number"].value == "699-12345-0000-000"
    assert extracted["so_number"].value == "SO-20260822-01"
    assert extracted["our_part_number"].value == "DGX-SPARK-001"
    assert extracted["quantity"].value == "2"
    assert extracted["net_weight"].value == "12.5 KG"
    assert extracted["gross_weight"].value == "15.0 KG"
    assert extracted["carton_number"].value == "3/10"
    assert "tracking_number" not in extracted
    assert "order_id" not in extracted


def test_dgx_spark_profile_does_not_invent_missing_fields():
    extracted = build_extractor("dgx_spark_label").extract(
        [OCRLine("NVIDIA DGX Spark", 0.99)],
        source="ppocr_v6",
    )

    assert all(item.value is None for item in extracted.values())
    assert all(item.reason == "NOT_FOUND" for item in extracted.values())
