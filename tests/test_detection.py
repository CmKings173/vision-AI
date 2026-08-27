from types import SimpleNamespace

import pytest

from label_inspection.detection.contour import ContourDetector, _geometric_confidence
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.detection.ultralytics_detector import UltralyticsLabelDetector


def frame(width=100, height=80):
    return SimpleNamespace(shape=(height, width, 3))


def test_fixed_roi_normalizes_and_clamps_coordinates():
    detector = FixedROIDetector((0.1, 0.25, 0.9, 0.75), normalized=True)

    candidates = detector.detect(frame(), frame_id=4)

    assert candidates[0].bbox == (10.0, 20.0, 90.0, 60.0)
    assert candidates[0].frame_id == 4


def test_fixed_roi_missing_or_malformed_configuration_fails_fast():
    with pytest.raises(ValueError, match="requires VISION_LABEL_ROI"):
        FixedROIDetector(None)
    with pytest.raises(ValueError, match="requires VISION_LABEL_ROI"):
        FixedROIDetector.parse_roi(None)
    with pytest.raises(ValueError):
        FixedROIDetector.parse_roi("1,2,3")


def test_fixed_roi_rejects_negative_or_reversed_coordinates():
    with pytest.raises(ValueError, match="must be >= 0"):
        FixedROIDetector.parse_roi("-1,0,20,20")
    with pytest.raises(ValueError, match="x2 > x1"):
        FixedROIDetector.parse_roi("20,0,10,20")


def test_fixed_roi_clamps_partial_overlap_and_errors_when_fully_outside():
    candidate = FixedROIDetector((80, 60, 120, 100), normalized=False).detect(frame())[0]
    assert candidate.bbox == (80.0, 60.0, 100.0, 80.0)

    with pytest.raises(ValueError, match="outside"):
        FixedROIDetector((200, 100, 300, 200), normalized=False).detect(frame())


def test_detector_support_scope_is_explicit_for_gx10_v1():
    assert FixedROIDetector.support_level == "SUPPORTED"
    assert ContourDetector.support_level == "EXPERIMENTAL"
    assert UltralyticsLabelDetector.support_level == "EXPERIMENTAL"


def test_contour_confidence_is_shape_based_not_frame_or_candidate_area():
    small = _geometric_confidence(800, 40, 25, is_quadrilateral=True)
    large = _geometric_confidence(8000, 400, 25, is_quadrilateral=True)

    assert small == large
