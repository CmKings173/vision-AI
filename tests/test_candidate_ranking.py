import time

import cv2
import numpy as np
import pytest

from label_inspection.camera.selector import FrameSelector
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.pipeline.inspection import InspectionPipeline
from label_inspection.pipeline.ranking import CandidateScorer, CandidateScoreWeights
from label_inspection.preprocessing.quality import QualityChecker, measure_quality
from label_inspection.schemas import FramePacket, LabelCandidate, OCRLine, RawOCRResult
from tests.fixtures.quality import blurred_label, sharp_label

pytestmark = pytest.mark.integration


ROI = (80, 60, 600, 280)


def frame_with_label(label: np.ndarray, *, sharp_background: bool) -> np.ndarray:
    if sharp_background:
        yy, xx = np.indices((360, 700))
        checker = (((xx // 4 + yy // 4) % 2) * 120 + 70).astype(np.uint8)
        frame = cv2.cvtColor(checker, cv2.COLOR_GRAY2BGR)
    else:
        frame = np.full((360, 700, 3), 150, dtype=np.uint8)
    frame[60:280, 80:600] = label
    return frame


class CountingOCR:
    engine = "ppocr"

    def __init__(self):
        self.calls = 0

    def recognize(self, image):
        self.calls += 1
        return RawOCRResult(engine=self.engine, lines=[OCRLine("SKU: ABC123", 0.97)])


def packets_for_ranking():
    now_wall = time.time()
    now_monotonic = time.monotonic()
    globally_sharp_label_blur = frame_with_label(blurred_label(), sharp_background=True)
    globally_plain_label_sharp = frame_with_label(sharp_label(), sharp_background=False)
    assert measure_quality(globally_sharp_label_blur).sharpness > measure_quality(globally_plain_label_sharp).sharpness
    return [
        FramePacket(1, now_wall, globally_sharp_label_blur, "test", now_monotonic),
        FramePacket(2, now_wall, globally_plain_label_sharp, "test", now_monotonic),
    ]


def test_best_label_crop_wins_even_when_global_frame_is_less_sharp():
    ocr = CountingOCR()
    pipeline = InspectionPipeline(
        detector=FixedROIDetector(ROI, normalized=False),
        ocr=ocr,
        selector=FrameSelector(
            top_k=2,
            score_fn=lambda frame: measure_quality(frame).sharpness or 0.0,
        ),
        quality_checker=QualityChecker(min_sharpness=50),
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        bbox_padding_ratio=0.0,
    )

    result = pipeline.inspect_packets(packets_for_ranking())

    assert result.frame_id == 2
    assert result.quality.status == "PASS"
    assert ocr.calls == 1


class ConfidenceDetector:
    def detect(self, frame, *, frame_id=None):
        confidence = 0.99 if frame_id == 1 else 0.85
        return [LabelCandidate(ROI, confidence=confidence, detector="test", frame_id=frame_id)]


def test_crop_quality_can_outweigh_slightly_higher_detection_confidence():
    ocr = CountingOCR()
    pipeline = InspectionPipeline(
        detector=ConfidenceDetector(),
        ocr=ocr,
        selector=FrameSelector(top_k=2, score_fn=lambda frame: 1.0),
        quality_checker=QualityChecker(min_sharpness=50),
        candidate_scorer=CandidateScorer(
            weights=CandidateScoreWeights(
                detection=0.20,
                sharpness=0.55,
                exposure=0.10,
                area=0.05,
                freshness=0.02,
                glare=0.03,
                validity=0.05,
            ),
            sharpness_reference=500,
        ),
        bbox_padding_ratio=0.0,
    )

    result = pipeline.inspect_packets(packets_for_ranking())

    assert result.frame_id == 2
    assert result.candidate_score.detection_confidence == 0.85
    assert ocr.calls == 1
