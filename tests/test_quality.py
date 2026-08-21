from label_inspection.preprocessing.quality import QualityChecker, measure_quality
from tests.fixtures.quality import blurred_label, dark_label, overexposed_label, sharp_label


def test_quality_flags_small_crop():
    report = QualityChecker(min_width=600, min_height=300).check(sharp_label())
    assert report.status == "FAIL"
    assert "LOW_RESOLUTION" in report.reasons


def test_real_opencv_sharpness_distinguishes_blur():
    sharp = measure_quality(sharp_label())
    blurred = measure_quality(blurred_label())
    assert sharp.sharpness > blurred.sharpness


def test_real_pixels_expose_dark_and_overexposed_ratios():
    normal = measure_quality(sharp_label())
    dark = measure_quality(dark_label())
    overexposed = measure_quality(overexposed_label())
    assert dark.underexposed_ratio > normal.underexposed_ratio
    assert overexposed.overexposed_ratio > normal.overexposed_ratio
    assert overexposed.glare_ratio > normal.glare_ratio


def test_quality_gate_uses_configured_thresholds():
    checker = QualityChecker(min_sharpness=50.0)
    assert checker.check(sharp_label()).status == "PASS"
    blurred = checker.check(blurred_label())
    assert blurred.status == "FAIL"
    assert "BLURRY" in blurred.reasons
