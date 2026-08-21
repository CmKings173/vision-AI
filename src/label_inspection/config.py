"""Environment-backed V2 configuration.

This module intentionally uses only the standard library so importing the
configuration never initializes CUDA, PaddleOCR, ZXing, or Ultralytics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only in minimal environments
    def load_dotenv() -> bool:
        return False


load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_text(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class Settings:
    camera_id: str = field(default_factory=lambda: os.getenv("VISION_CAMERA_ID", "PHONE-01"))
    rtsp_url: Optional[str] = field(default_factory=lambda: _optional_text("VISION_RTSP_URL"))
    buffer_size: int = field(default_factory=lambda: int(os.getenv("VISION_BUFFER_SIZE", "8")))
    buffer_window_ms: int = field(
        default_factory=lambda: int(os.getenv("VISION_BUFFER_WINDOW_MS", "800"))
    )
    max_frame_age_ms: int = field(
        default_factory=lambda: int(os.getenv("VISION_MAX_FRAME_AGE_MS", "1000"))
    )
    rtsp_open_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("VISION_RTSP_OPEN_TIMEOUT_MS", "5000"))
    )
    rtsp_read_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("VISION_RTSP_READ_TIMEOUT_MS", "2000"))
    )
    top_k: int = field(default_factory=lambda: int(os.getenv("VISION_TOP_K", "3")))
    frame_preview_long_edge: int = field(
        default_factory=lambda: int(os.getenv("VISION_FRAME_PREVIEW_LONG_EDGE", "480"))
    )
    label_roi: Optional[str] = field(default_factory=lambda: _optional_text("VISION_LABEL_ROI"))
    roi_normalized: bool = field(
        default_factory=lambda: _bool("VISION_ROI_NORMALIZED", True)
    )
    bbox_padding_ratio: float = field(
        default_factory=lambda: float(os.getenv("VISION_BBOX_PADDING_RATIO", "0.05"))
    )
    detector: str = field(default_factory=lambda: os.getenv("VISION_DETECTOR", "FixedROI"))
    detector_model: str = field(
        default_factory=lambda: os.getenv("VISION_DETECTOR_MODEL", "models/shipping_label.pt")
    )
    detector_device: str = field(
        default_factory=lambda: os.getenv("VISION_DETECTOR_DEVICE", "cpu")
    )
    ocr_device: str = field(default_factory=lambda: os.getenv("VISION_OCR_DEVICE", "cpu"))
    ocr_engine: str = field(default_factory=lambda: os.getenv("VISION_OCR_ENGINE", "ppocr"))
    ocr_lang: str = field(default_factory=lambda: os.getenv("VISION_OCR_LANG", "en"))
    ocr_confidence: float = field(
        default_factory=lambda: float(os.getenv("VISION_OCR_CONFIDENCE", "0.70"))
    )
    barcode_engine: str = field(
        default_factory=lambda: os.getenv("VISION_BARCODE_ENGINE", "zxing")
    )
    required_fields: tuple[str, ...] = field(default_factory=lambda: _required_fields())
    barcode_required: bool = field(
        default_factory=lambda: _bool("VISION_BARCODE_REQUIRED", False)
    )
    quality_min_width: int = field(
        default_factory=lambda: int(os.getenv("VISION_QUALITY_MIN_WIDTH", "32"))
    )
    quality_min_height: int = field(
        default_factory=lambda: int(os.getenv("VISION_QUALITY_MIN_HEIGHT", "16"))
    )
    quality_min_sharpness: float = field(
        default_factory=lambda: float(os.getenv("VISION_QUALITY_MIN_SHARPNESS", "50"))
    )
    quality_min_brightness: float = field(
        default_factory=lambda: float(os.getenv("VISION_QUALITY_MIN_BRIGHTNESS", "20"))
    )
    quality_max_brightness: float = field(
        default_factory=lambda: float(os.getenv("VISION_QUALITY_MAX_BRIGHTNESS", "245"))
    )
    quality_max_underexposed_ratio: float = field(
        default_factory=lambda: float(os.getenv("VISION_QUALITY_MAX_UNDEREXPOSED_RATIO", "0.30"))
    )
    quality_max_overexposed_ratio: float = field(
        default_factory=lambda: float(os.getenv("VISION_QUALITY_MAX_OVEREXPOSED_RATIO", "0.30"))
    )
    quality_max_glare_ratio: float = field(
        default_factory=lambda: float(os.getenv("VISION_QUALITY_MAX_GLARE_RATIO", "0.20"))
    )
    candidate_sharpness_reference: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_SHARPNESS_REFERENCE", "500"))
    )
    score_weight_detection: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_DETECTION", "0.25"))
    )
    score_weight_sharpness: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_SHARPNESS", "0.35"))
    )
    score_weight_exposure: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_EXPOSURE", "0.15"))
    )
    score_weight_area: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_AREA", "0.05"))
    )
    score_weight_freshness: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_FRESHNESS", "0.05"))
    )
    score_weight_glare: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_GLARE", "0.05"))
    )
    score_weight_validity: float = field(
        default_factory=lambda: float(os.getenv("VISION_SCORE_WEIGHT_VALIDITY", "0.10"))
    )
    log_level: str = field(default_factory=lambda: os.getenv("VISION_LOG_LEVEL", "INFO"))

    def validate(self) -> None:
        if self.buffer_size < 1:
            raise ValueError("VISION_BUFFER_SIZE must be >= 1")
        if self.top_k < 1:
            raise ValueError("VISION_TOP_K must be >= 1")
        if self.top_k > self.buffer_size:
            raise ValueError("VISION_TOP_K cannot exceed VISION_BUFFER_SIZE")
        if not 320 <= self.frame_preview_long_edge <= 640:
            raise ValueError("VISION_FRAME_PREVIEW_LONG_EDGE must be between 320 and 640")
        if not 0 <= self.ocr_confidence <= 1:
            raise ValueError("VISION_OCR_CONFIDENCE must be between 0 and 1")
        if self.max_frame_age_ms < 1:
            raise ValueError("VISION_MAX_FRAME_AGE_MS must be >= 1")
        if self.rtsp_open_timeout_ms < 1 or self.rtsp_read_timeout_ms < 1:
            raise ValueError("RTSP timeout values must be >= 1 ms")
        detector = self.detector.strip().lower().replace("_", "-")
        if detector not in {"fixedroi", "fixed-roi", "roi", "contour", "contours", "ultralytics", "yolo"}:
            raise ValueError(f"unsupported detector: {self.detector}")
        if detector in {"fixedroi", "fixed-roi", "roi"}:
            from .detection.fixed_roi import FixedROIDetector

            FixedROIDetector.parse_roi(self.label_roi)
        if self.ocr_engine.strip().lower() != "ppocr":
            raise ValueError("VISION_OCR_ENGINE must be 'ppocr' for V1")
        if self.barcode_engine.strip().lower() != "zxing":
            raise ValueError("VISION_BARCODE_ENGINE must be 'zxing' for V1")
        ratios = (
            self.quality_max_underexposed_ratio,
            self.quality_max_overexposed_ratio,
            self.quality_max_glare_ratio,
        )
        if any(value < 0 or value > 1 for value in ratios):
            raise ValueError("quality ratio thresholds must be between 0 and 1")
        weights = (
            self.score_weight_detection,
            self.score_weight_sharpness,
            self.score_weight_exposure,
            self.score_weight_area,
            self.score_weight_freshness,
            self.score_weight_glare,
            self.score_weight_validity,
        )
        if any(value < 0 for value in weights) or sum(weights) <= 0:
            raise ValueError("candidate score weights must be non-negative with a positive sum")


def _required_fields() -> tuple[str, ...]:
    raw = os.getenv("VISION_REQUIRED_FIELDS", "sku")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


settings = Settings()
