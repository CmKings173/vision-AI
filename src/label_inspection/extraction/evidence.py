"""Profile-independent evidence inventory for inspection results."""

from __future__ import annotations

from collections.abc import Iterable

from ..schemas import BarcodeResult, EvidenceItem, RawOCRResult


def collect_evidence(
    raw_ocr: RawOCRResult,
    barcodes: Iterable[BarcodeResult],
) -> list[EvidenceItem]:
    """Collect raw observations without assigning business semantics.

    OCR lines and barcode observations are retained exactly as stage outputs.
    This function intentionally does not parse labels, normalize field names,
    or select a document profile.
    """

    evidence = [
        EvidenceItem(
            kind="OCR_LINE",
            text=line.text,
            confidence=float(line.confidence),
            source=raw_ocr.engine,
            polygon=line.polygon,
        )
        for line in raw_ocr.lines
    ]
    for barcode in barcodes:
        evidence.append(
            EvidenceItem(
                kind="BARCODE",
                text=barcode.value,
                confidence=barcode.confidence,
                source="barcode",
                metadata={
                    "format": barcode.format,
                    "valid": barcode.valid,
                    "state": barcode.state,
                    "error": barcode.error,
                    "error_code": barcode.error_code,
                },
            )
        )
    return evidence
