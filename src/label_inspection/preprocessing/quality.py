"""Cheap crop quality checks used both for frame selection and validation."""

from __future__ import annotations

from dataclasses import replace
from ..schemas import STAGE_FAILED, QualityReport
from ..detection.fixed_roi import frame_size


def measure_quality(image: object) -> QualityReport:
    width, height = frame_size(image)
    if width <= 0 or height <= 0:
        return QualityReport(status="FAIL", width=width, height=height, reasons=("EMPTY_CROP",))
    try:
        import numpy as np

        array = np.asarray(image)
        if array.size == 0:
            return QualityReport(status="FAIL", width=width, height=height, area=0, reasons=("EMPTY_CROP",))
        if array.ndim == 3:
            try:
                import cv2

                gray = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
            except ImportError:
                gray = array.mean(axis=2)
        else:
            gray = array
        brightness = float(gray.mean())
        underexposed_ratio = float((gray <= 30).mean())
        overexposed_ratio = float((gray >= 245).mean())
        glare_ratio = float((gray >= 252).mean())
        try:
            import cv2

            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except ImportError:
            sharpness = float(gray.var())
        return QualityReport(
            status="PASS",
            sharpness=sharpness,
            brightness=brightness,
            underexposed_ratio=underexposed_ratio,
            overexposed_ratio=overexposed_ratio,
            glare_ratio=glare_ratio,
            width=width,
            height=height,
            area=width * height,
        )
    except (ImportError, TypeError, ValueError, AttributeError):
        return QualityReport(
            status="ERROR",
            state=STAGE_FAILED,
            width=width,
            height=height,
            area=width * height,
            reasons=("METRICS_UNAVAILABLE",),
        )


class QualityChecker:
    def __init__(
        self,
        *,
        min_width: int = 32,
        min_height: int = 16,
        min_brightness: float = 20.0,
        max_brightness: float = 245.0,
        min_sharpness: float = 50.0,
        max_underexposed_ratio: float = 0.30,
        max_overexposed_ratio: float = 0.30,
        max_glare_ratio: float = 0.20,
    ) -> None:
        self.min_width = min_width
        self.min_height = min_height
        self.min_brightness = min_brightness
        self.max_brightness = max_brightness
        self.min_sharpness = min_sharpness
        self.max_underexposed_ratio = max_underexposed_ratio
        self.max_overexposed_ratio = max_overexposed_ratio
        self.max_glare_ratio = max_glare_ratio

    def check(self, image: object) -> QualityReport:
        measured = measure_quality(image)
        if measured.state == STAGE_FAILED:
            return measured
        reasons = list(measured.reasons)
        if (measured.width or 0) < self.min_width or (measured.height or 0) < self.min_height:
            reasons.append("LOW_RESOLUTION")
        if measured.brightness is not None:
            if measured.brightness < self.min_brightness:
                reasons.append("LOW_LIGHT")
            elif measured.brightness > self.max_brightness:
                reasons.append("OVEREXPOSED")
        if measured.sharpness is not None and measured.sharpness < self.min_sharpness:
            reasons.append("BLURRY")
        if (
            measured.underexposed_ratio is not None
            and measured.underexposed_ratio > self.max_underexposed_ratio
        ):
            reasons.append("UNDEREXPOSED")
        if (
            measured.overexposed_ratio is not None
            and measured.overexposed_ratio > self.max_overexposed_ratio
        ):
            reasons.append("OVEREXPOSED_RATIO")
        if measured.glare_ratio is not None and measured.glare_ratio > self.max_glare_ratio:
            reasons.append("GLARE")
        return replace(measured, status="FAIL" if reasons else "PASS", reasons=tuple(dict.fromkeys(reasons)))
