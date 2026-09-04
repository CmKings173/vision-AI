from label_inspection.pipeline.types import PreparedInspection
from label_inspection.schemas import (
    BarcodeResult,
    LabelCandidate,
    LabelCandidateScore,
    OCRLine,
    QualityReport,
    RawOCRResult,
)
from label_inspection.worker.processor import InspectionProcessor


class ArbitraryOCR:
    engine = "fake-ocr"

    def recognize(self, image):
        return RawOCRResult(
            engine=self.engine,
            lines=[OCRLine("CUSTOMER LABEL / UNKNOWN FORMAT", 0.96)],
        )


class ArbitraryBarcode:
    def decode(self, image):
        return [BarcodeResult(value="UNKNOWN-DOC-001", format="CODE128", valid=True)]


def _prepared():
    return PreparedInspection(
        event_id="event",
        trigger_id="trigger",
        station_id="station",
        camera_id="camera",
        triggered_at_ms=1000,
        received_at_ms=1001,
        prepared_at_ms=1002,
        selected_frame=object(),
        label_crop=object(),
        frame_id=1,
        label=LabelCandidate(bbox=(0, 0, 10, 10)),
        crop_bbox=(0, 0, 10, 10),
        candidate_score=LabelCandidateScore(
            total=1,
            detection_confidence=1,
            crop_sharpness=1,
            crop_exposure=1,
            crop_glare=1,
            label_area_ratio=1,
            frame_freshness=1,
            crop_validity=1,
        ),
        quality=QualityReport(status="PASS"),
        timing={},
    )


def test_unprofiled_processor_preserves_unknown_document_evidence_and_reviews():
    result = InspectionProcessor(
        ocr=ArbitraryOCR(),
        barcode=ArbitraryBarcode(),
    ).process(_prepared())

    assert result.extracted == {}
    assert result.validation.status == "REVIEW"
    assert "NO_APPROVED_PROFILE" in result.validation.reasons
    assert [item.text for item in result.evidence] == [
        "CUSTOMER LABEL / UNKNOWN FORMAT",
        "UNKNOWN-DOC-001",
    ]
    assert result.to_dict()["evidence"][0]["text"] == "CUSTOMER LABEL / UNKNOWN FORMAT"
