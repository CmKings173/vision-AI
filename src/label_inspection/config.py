"""Environment-backed V2 configuration.

This module intentionally uses only the standard library so importing the
configuration never initializes CUDA, PaddleOCR, ZXing, or Ultralytics.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only in minimal environments
    def load_dotenv() -> bool:
        return False


load_dotenv()


class ConfigError(ValueError):
    """Raised when environment or explicit runtime configuration is unsafe."""


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ConfigError(
        f"{name} must be one of true, false, 1, 0, yes, or no"
    )


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float(
    name: str,
    default: float,
    *,
    greater_than: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw.strip())
        except (AttributeError, TypeError, ValueError) as exc:
            raise ConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
    if greater_than is not None and value <= greater_than:
        raise ConfigError(f"{name} must be > {greater_than}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return value


def _optional_text(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class Settings:
    station_id: str = field(
        default_factory=lambda: os.getenv("VISION_STATION_ID", "STATION-01")
    )
    camera_id: str = field(default_factory=lambda: os.getenv("VISION_CAMERA_ID", "PHONE-01"))
    rtsp_url: str | None = field(default_factory=lambda: _optional_text("VISION_RTSP_URL"))
    spool_root: str = field(
        default_factory=lambda: os.getenv("VISION_SPOOL_ROOT", "spool")
    )
    spool_max_pending_events: int = field(
        default_factory=lambda: _int("VISION_SPOOL_MAX_PENDING_EVENTS", 1000)
    )
    spool_max_pending_bytes: int = field(
        default_factory=lambda: _int(
            "VISION_SPOOL_MAX_PENDING_BYTES", 10_737_418_240
        )
    )
    spool_min_free_disk_bytes: int = field(
        default_factory=lambda: _int(
            "VISION_SPOOL_MIN_FREE_DISK_BYTES", 2_147_483_648
        )
    )
    artifact_bucket: str = field(
        default_factory=lambda: os.getenv("VISION_ARTIFACT_BUCKET", "vision-inspections")
    )
    minio_endpoint: str | None = field(
        default_factory=lambda: _optional_text("VISION_MINIO_ENDPOINT")
    )
    minio_access_key: str | None = field(
        default_factory=lambda: _optional_text("VISION_MINIO_ACCESS_KEY"), repr=False
    )
    minio_secret_key: str | None = field(
        default_factory=lambda: _optional_text("VISION_MINIO_SECRET_KEY"), repr=False
    )
    minio_secure: bool = field(
        default_factory=lambda: _bool("VISION_MINIO_SECURE", False)
    )
    rabbitmq_url: str | None = field(
        default_factory=lambda: _optional_text("VISION_RABBITMQ_URL"), repr=False
    )
    dispatch_interval_s: float = field(
        default_factory=lambda: _float(
            "VISION_DISPATCH_INTERVAL_S", 1.0, greater_than=0.0, maximum=60.0
        )
    )
    max_job_message_bytes: int = field(
        default_factory=lambda: _int("VISION_MAX_JOB_MESSAGE_BYTES", 1_048_576)
    )
    max_label_crop_bytes: int = field(
        default_factory=lambda: _int("VISION_MAX_LABEL_CROP_BYTES", 16_777_216)
    )
    max_image_pixels: int = field(
        default_factory=lambda: _int("VISION_MAX_IMAGE_PIXELS", 16_000_000)
    )
    retry_delays_ms: tuple[int, ...] = field(
        default_factory=lambda: _retry_delays()
    )
    buffer_size: int = field(default_factory=lambda: _int("VISION_BUFFER_SIZE", 8))
    buffer_window_ms: int = field(
        default_factory=lambda: _int("VISION_BUFFER_WINDOW_MS", 800)
    )
    max_frame_age_ms: int = field(
        default_factory=lambda: _int("VISION_MAX_FRAME_AGE_MS", 1000)
    )
    rtsp_open_timeout_ms: int = field(
        default_factory=lambda: _int("VISION_RTSP_OPEN_TIMEOUT_MS", 5000)
    )
    rtsp_read_timeout_ms: int = field(
        default_factory=lambda: _int("VISION_RTSP_READ_TIMEOUT_MS", 2000)
    )
    camera_rotate_degrees: int = field(
        default_factory=lambda: _int("VISION_CAMERA_ROTATE_DEG", 0)
    )
    top_k: int = field(default_factory=lambda: _int("VISION_TOP_K", 3))
    frame_preview_long_edge: int = field(
        default_factory=lambda: _int("VISION_FRAME_PREVIEW_LONG_EDGE", 480)
    )
    label_roi: str | None = field(default_factory=lambda: _optional_text("VISION_LABEL_ROI"))
    roi_normalized: bool = field(
        default_factory=lambda: _bool("VISION_ROI_NORMALIZED", True)
    )
    bbox_padding_ratio: float = field(
        default_factory=lambda: _float("VISION_BBOX_PADDING_RATIO", 0.05)
    )
    detector: str = field(default_factory=lambda: os.getenv("VISION_DETECTOR", "FixedROI"))
    detector_model: str = field(
        default_factory=lambda: os.getenv("VISION_DETECTOR_MODEL", "models/shipping_label.pt")
    )
    detector_device: str = field(
        default_factory=lambda: os.getenv("VISION_DETECTOR_DEVICE", "cpu")
    )
    detector_confidence: float = field(
        default_factory=lambda: _float("VISION_DETECTOR_CONFIDENCE", 0.25)
    )
    detector_iou: float = field(
        default_factory=lambda: _float("VISION_DETECTOR_IOU", 0.45)
    )
    detector_image_size: int = field(
        default_factory=lambda: _int("VISION_DETECTOR_IMGSZ", 640)
    )
    detector_max_det: int = field(
        default_factory=lambda: _int("VISION_DETECTOR_MAX_DET", 10)
    )
    ocr_device: str = field(default_factory=lambda: os.getenv("VISION_OCR_DEVICE", "cpu"))
    ocr_engine: str = field(default_factory=lambda: os.getenv("VISION_OCR_ENGINE", "ppocr"))
    ocr_backend: str = field(
        default_factory=lambda: os.getenv("VISION_OCR_BACKEND", "transformers")
    )
    ocr_version: str = field(
        default_factory=lambda: os.getenv("VISION_OCR_VERSION", "PP-OCRv6")
    )
    ocr_lang: str = field(default_factory=lambda: os.getenv("VISION_OCR_LANG", "en"))
    ocr_det_engine: str | None = field(
        default_factory=lambda: _optional_text("VISION_OCR_DET_ENGINE")
    )
    ocr_rec_engine: str | None = field(
        default_factory=lambda: _optional_text("VISION_OCR_REC_ENGINE")
    )
    ocr_cls_engine: str | None = field(
        default_factory=lambda: _optional_text("VISION_OCR_CLS_ENGINE")
    )
    ocr_char_dict: str | None = field(
        default_factory=lambda: _optional_text("VISION_OCR_CHAR_DICT")
    )
    ocr_det_input_height: int = field(
        default_factory=lambda: _int("VISION_OCR_DET_INPUT_HEIGHT", 960)
    )
    ocr_det_input_width: int = field(
        default_factory=lambda: _int("VISION_OCR_DET_INPUT_WIDTH", 960)
    )
    ocr_rec_image_height: int = field(
        default_factory=lambda: _int("VISION_OCR_REC_IMAGE_HEIGHT", 48)
    )
    ocr_rec_image_width: int = field(
        default_factory=lambda: _int("VISION_OCR_REC_IMAGE_WIDTH", 320)
    )
    ocr_det_threshold: float = field(
        default_factory=lambda: _float("VISION_OCR_DET_THRESHOLD", 0.30)
    )
    ocr_det_box_threshold: float = field(
        default_factory=lambda: _float("VISION_OCR_DET_BOX_THRESHOLD", 0.60)
    )
    ocr_det_min_box_size: int = field(
        default_factory=lambda: _int("VISION_OCR_DET_MIN_BOX_SIZE", 3)
    )
    ocr_confidence: float = field(
        default_factory=lambda: _float("VISION_OCR_CONFIDENCE", 0.70)
    )
    barcode_engine: str = field(
        default_factory=lambda: os.getenv("VISION_BARCODE_ENGINE", "zxing")
    )
    required_fields: tuple[str, ...] = field(default_factory=lambda: _required_fields())
    extraction_profile: str = field(
        default_factory=lambda: os.getenv("VISION_EXTRACTION_PROFILE", "default")
    )
    barcode_required: bool = field(
        default_factory=lambda: _bool("VISION_BARCODE_REQUIRED", False)
    )
    quality_min_width: int = field(
        default_factory=lambda: _int("VISION_QUALITY_MIN_WIDTH", 32)
    )
    quality_min_height: int = field(
        default_factory=lambda: _int("VISION_QUALITY_MIN_HEIGHT", 16)
    )
    quality_min_sharpness: float = field(
        default_factory=lambda: _float("VISION_QUALITY_MIN_SHARPNESS", 50.0)
    )
    quality_min_brightness: float = field(
        default_factory=lambda: _float("VISION_QUALITY_MIN_BRIGHTNESS", 20.0)
    )
    quality_max_brightness: float = field(
        default_factory=lambda: _float("VISION_QUALITY_MAX_BRIGHTNESS", 245.0)
    )
    quality_max_underexposed_ratio: float = field(
        default_factory=lambda: _float("VISION_QUALITY_MAX_UNDEREXPOSED_RATIO", 0.30)
    )
    quality_max_overexposed_ratio: float = field(
        default_factory=lambda: _float("VISION_QUALITY_MAX_OVEREXPOSED_RATIO", 0.30)
    )
    quality_max_glare_ratio: float = field(
        default_factory=lambda: _float("VISION_QUALITY_MAX_GLARE_RATIO", 0.20)
    )
    candidate_sharpness_reference: float = field(
        default_factory=lambda: _float("VISION_SCORE_SHARPNESS_REFERENCE", 500.0)
    )
    score_weight_detection: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_DETECTION", 0.25)
    )
    score_weight_sharpness: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_SHARPNESS", 0.35)
    )
    score_weight_exposure: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_EXPOSURE", 0.15)
    )
    score_weight_area: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_AREA", 0.05)
    )
    score_weight_freshness: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_FRESHNESS", 0.05)
    )
    score_weight_glare: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_GLARE", 0.05)
    )
    score_weight_validity: float = field(
        default_factory=lambda: _float("VISION_SCORE_WEIGHT_VALIDITY", 0.10)
    )
    log_level: str = field(default_factory=lambda: os.getenv("VISION_LOG_LEVEL", "INFO"))

    def validate(self) -> None:
        """Validate the complete local compatibility pipeline configuration."""

        self.validate_station()
        self.validate_worker()

    def validate_station(self) -> None:
        """Validate only capture/preparation settings owned by station-service."""

        for name, value in (
            ("VISION_BBOX_PADDING_RATIO", self.bbox_padding_ratio),
            ("VISION_QUALITY_MIN_SHARPNESS", self.quality_min_sharpness),
            ("VISION_QUALITY_MIN_BRIGHTNESS", self.quality_min_brightness),
            ("VISION_QUALITY_MAX_BRIGHTNESS", self.quality_max_brightness),
            (
                "VISION_QUALITY_MAX_UNDEREXPOSED_RATIO",
                self.quality_max_underexposed_ratio,
            ),
            (
                "VISION_QUALITY_MAX_OVEREXPOSED_RATIO",
                self.quality_max_overexposed_ratio,
            ),
            ("VISION_QUALITY_MAX_GLARE_RATIO", self.quality_max_glare_ratio),
            (
                "VISION_SCORE_SHARPNESS_REFERENCE",
                self.candidate_sharpness_reference,
            ),
            ("VISION_SCORE_WEIGHT_DETECTION", self.score_weight_detection),
            ("VISION_SCORE_WEIGHT_SHARPNESS", self.score_weight_sharpness),
            ("VISION_SCORE_WEIGHT_EXPOSURE", self.score_weight_exposure),
            ("VISION_SCORE_WEIGHT_AREA", self.score_weight_area),
            ("VISION_SCORE_WEIGHT_FRESHNESS", self.score_weight_freshness),
            ("VISION_SCORE_WEIGHT_GLARE", self.score_weight_glare),
            ("VISION_SCORE_WEIGHT_VALIDITY", self.score_weight_validity),
            ("VISION_DETECTOR_CONFIDENCE", self.detector_confidence),
            ("VISION_DETECTOR_IOU", self.detector_iou),
        ):
            _require_finite(name, value)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.station_id):
            raise ValueError(
                "VISION_STATION_ID must match [A-Za-z0-9_-]{1,128}"
            )
        if not self.camera_id.strip():
            raise ValueError("VISION_CAMERA_ID must not be empty")
        if not self.spool_root.strip():
            raise ValueError("VISION_SPOOL_ROOT must not be empty")
        if self.spool_max_pending_events < 1:
            raise ValueError("VISION_SPOOL_MAX_PENDING_EVENTS must be >= 1")
        if self.spool_max_pending_bytes < 1:
            raise ValueError("VISION_SPOOL_MAX_PENDING_BYTES must be >= 1")
        if self.spool_min_free_disk_bytes < 0:
            raise ValueError("VISION_SPOOL_MIN_FREE_DISK_BYTES must be >= 0")
        if self.buffer_size < 1:
            raise ValueError("VISION_BUFFER_SIZE must be >= 1")
        if self.top_k < 1:
            raise ValueError("VISION_TOP_K must be >= 1")
        if self.top_k > self.buffer_size:
            raise ValueError("VISION_TOP_K cannot exceed VISION_BUFFER_SIZE")
        if not 320 <= self.frame_preview_long_edge <= 640:
            raise ValueError("VISION_FRAME_PREVIEW_LONG_EDGE must be between 320 and 640")
        if self.max_frame_age_ms < 1:
            raise ValueError("VISION_MAX_FRAME_AGE_MS must be >= 1")
        if self.rtsp_open_timeout_ms < 1 or self.rtsp_read_timeout_ms < 1:
            raise ValueError("RTSP timeout values must be >= 1 ms")
        if self.camera_rotate_degrees not in {0, 90, 180, 270}:
            raise ValueError("VISION_CAMERA_ROTATE_DEG must be one of 0, 90, 180, or 270")
        detector = self.detector.strip().lower().replace("_", "-")
        if detector not in {"fixedroi", "fixed-roi", "roi", "contour", "contours", "ultralytics", "yolo"}:
            raise ValueError(f"unsupported detector: {self.detector}")
        if detector in {"fixedroi", "fixed-roi", "roi"}:
            from .detection.fixed_roi import FixedROIDetector

            FixedROIDetector.parse_roi(self.label_roi)
        if not 0 <= self.detector_confidence <= 1:
            raise ValueError("VISION_DETECTOR_CONFIDENCE must be between 0 and 1")
        if not 0 <= self.detector_iou <= 1:
            raise ValueError("VISION_DETECTOR_IOU must be between 0 and 1")
        if (
            isinstance(self.detector_image_size, bool)
            or not isinstance(self.detector_image_size, int)
            or self.detector_image_size < 1
        ):
            raise ValueError("VISION_DETECTOR_IMGSZ must be a positive integer")
        if (
            isinstance(self.detector_max_det, bool)
            or not isinstance(self.detector_max_det, int)
            or not 1 <= self.detector_max_det <= 1000
        ):
            raise ValueError("VISION_DETECTOR_MAX_DET must be between 1 and 1000")
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

    def validate_worker(self) -> None:
        """Validate only inference and business-processing settings."""

        for name, value in (
            ("VISION_OCR_CONFIDENCE", self.ocr_confidence),
            ("VISION_OCR_DET_THRESHOLD", self.ocr_det_threshold),
            ("VISION_OCR_DET_BOX_THRESHOLD", self.ocr_det_box_threshold),
        ):
            _require_finite(name, value)
        if not 0 <= self.ocr_confidence <= 1:
            raise ValueError("VISION_OCR_CONFIDENCE must be between 0 and 1")
        ocr_engine = self.ocr_engine.strip().lower().replace("_", "-")
        if ocr_engine not in {"ppocr", "ppocr-v6", "tensorrt", "tensor-rt"}:
            raise ValueError(
                "VISION_OCR_ENGINE must be 'ppocr', 'ppocr_v6', or 'tensorrt'"
            )
        if ocr_engine == "ppocr-v6":
            if self.ocr_backend.strip().lower() != "transformers":
                raise ValueError("PP-OCRv6 requires VISION_OCR_BACKEND=transformers")
            if self.ocr_version.strip().lower() != "pp-ocrv6":
                raise ValueError("PP-OCRv6 requires VISION_OCR_VERSION=PP-OCRv6")
        if ocr_engine in {"tensorrt", "tensor-rt"}:
            missing = [
                name
                for name, value in (
                    ("VISION_OCR_DET_ENGINE", self.ocr_det_engine),
                    ("VISION_OCR_REC_ENGINE", self.ocr_rec_engine),
                    ("VISION_OCR_CHAR_DICT", self.ocr_char_dict),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "TensorRT OCR requires " + ", ".join(missing)
                )
            if self.ocr_det_input_height < 1 or self.ocr_det_input_width < 1:
                raise ValueError("TensorRT OCR detection input dimensions must be >= 1")
            if self.ocr_rec_image_height < 1 or self.ocr_rec_image_width < 1:
                raise ValueError("TensorRT OCR recognition input dimensions must be >= 1")
            if not 0 <= self.ocr_det_threshold <= 1:
                raise ValueError("VISION_OCR_DET_THRESHOLD must be between 0 and 1")
            if not 0 <= self.ocr_det_box_threshold <= 1:
                raise ValueError("VISION_OCR_DET_BOX_THRESHOLD must be between 0 and 1")
            if self.ocr_det_min_box_size < 1:
                raise ValueError("VISION_OCR_DET_MIN_BOX_SIZE must be >= 1")
        if self.barcode_engine.strip().lower() != "zxing":
            raise ValueError("VISION_BARCODE_ENGINE must be 'zxing' for V1")
        profile = self.extraction_profile.strip().lower().replace("-", "_")
        if profile not in {"default", "dgx_spark", "dgx_spark_label"}:
            raise ValueError(
                "VISION_EXTRACTION_PROFILE must be 'default' or 'dgx_spark_label'"
            )

    def validate_phase2_transport(self) -> None:
        if not self.artifact_bucket.strip():
            raise ValueError("VISION_ARTIFACT_BUCKET must not be empty")
        for name, value in (
            ("VISION_MINIO_ENDPOINT", self.minio_endpoint),
            ("VISION_MINIO_ACCESS_KEY", self.minio_access_key),
            ("VISION_MINIO_SECRET_KEY", self.minio_secret_key),
            ("VISION_RABBITMQ_URL", self.rabbitmq_url),
        ):
            if value is None or not value.strip():
                raise ValueError(f"{name} is required for Phase 2")
        if not self.rabbitmq_url.lower().startswith(("amqp://", "amqps://")):
            raise ValueError("VISION_RABBITMQ_URL must use amqp:// or amqps://")
        interval = _require_finite(
            "VISION_DISPATCH_INTERVAL_S", self.dispatch_interval_s
        )
        if interval <= 0 or interval > 60:
            raise ConfigError(
                "VISION_DISPATCH_INTERVAL_S must be > 0 and <= 60"
            )
        for name, value, maximum in (
            ("VISION_MAX_JOB_MESSAGE_BYTES", self.max_job_message_bytes, 16_777_216),
            (
                "VISION_MAX_LABEL_CROP_BYTES",
                self.max_label_crop_bytes,
                134_217_728,
            ),
            ("VISION_MAX_IMAGE_PIXELS", self.max_image_pixels, 100_000_000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > maximum
            ):
                raise ConfigError(f"{name} must be between 1 and {maximum}")
        if (
            not self.retry_delays_ms
            or len(self.retry_delays_ms) > 10
            or any(delay < 1 for delay in self.retry_delays_ms)
            or any(
                later <= earlier
                for earlier, later in zip(
                    self.retry_delays_ms, self.retry_delays_ms[1:]
                )
            )
        ):
            raise ValueError(
                "VISION_RETRY_DELAYS_MS must contain 1-10 increasing positive integers"
            )

    def validate_phase2_station(self) -> None:
        self.validate_station()
        self.validate_phase2_transport()

    def validate_phase2_worker(self) -> None:
        self.validate_worker()
        self.validate_phase2_transport()
        if self.ocr_engine.strip().lower().replace("_", "-") != "ppocr-v6":
            raise ValueError("Phase 2 worker requires PP-OCRv6 resident lifecycle")


def _required_fields() -> tuple[str, ...]:
    raw = os.getenv("VISION_REQUIRED_FIELDS", "sku")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _retry_delays() -> tuple[int, ...]:
    raw = os.getenv("VISION_RETRY_DELAYS_MS", "5000,30000,120000")
    try:
        return tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError(
            "VISION_RETRY_DELAYS_MS must contain comma-separated integers"
        ) from exc


def _require_finite(name: str, value: object) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(numeric):
        raise ConfigError(f"{name} must be finite")
    return numeric


settings = Settings()
