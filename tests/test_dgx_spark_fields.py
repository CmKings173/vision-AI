from label_inspection.extraction.profiles import (
    DGX_SPARK_LABEL_FIELDS,
    build_extractor,
)
from label_inspection.schemas import OCRLine


def test_unapproved_dgx_profile_does_not_emit_canonical_business_fields():
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
    assert extracted == {}


def test_dgx_spark_profile_does_not_invent_missing_fields():
    extracted = build_extractor("dgx_spark_label").extract(
        [OCRLine("NVIDIA DGX Spark", 0.99)],
        source="ppocr_v6",
    )

    assert extracted == {}


def test_dgx_spark_profile_maps_split_label_value_lines_and_label_aliases():
    lines = [
        OCRLine("Nvidia P/N:940-54242-0006-000", 0.99),
        OCRLine("S/O NO.: SOA-250900131-1", 0.99),
        OCRLine("Carton ID : 198MA5020450", 0.96),
        OCRLine("Q'TY:", 0.99),
        OCRLine("2", 0.99),
        OCRLine("N.W.:", 0.99),
        OCRLine("5.240", 0.99),
        OCRLine("G.W.:", 0.99),
        OCRLine("7.240", 0.99),
        OCRLine("C/NO.:", 0.99),
        OCRLine("B0027", 0.99),
    ]

    extracted = build_extractor("dgx_spark_label").extract(
        lines,
        source="ppocr_v6",
    )

    assert extracted == {}


def test_dgx_spark_profile_preserves_alias_and_declares_semantic_blocker():
    extractor = build_extractor("dgx_spark_label")
    extracted = extractor.extract(
        [OCRLine("Nvidia P/N:940-54242-0006-000", 0.99)],
        source="ppocr_v6",
    )

    assert extracted == {}
    assert extractor.profile_name == "dgx_spark_label"
    assert extractor.profile_version
    assert extractor.semantic_blockers["customer_part_number"].startswith(
        "KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION"
    )
    assert "Nvidia P/N" in extractor.mapping_summary["customer_part_number"]


def test_dgx_spark_profile_does_not_capture_customer_part_label_as_value():
    extracted = build_extractor("dgx_spark_label").extract(
        [
            OCRLine("Customer Part Number", 0.99),
            OCRLine("126X600000A", 0.99),
        ],
        source="ppocr_v6",
    )

    assert extracted == {}
