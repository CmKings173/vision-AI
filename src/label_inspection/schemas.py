"""Stable domain and JSON schemas for label inspection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .contracts.core import freeze_json, thaw_json

STAGE_NOT_RUN = "NOT_RUN"
STAGE_SUCCESS = "SUCCESS"
STAGE_FAILED = "FAILED"
STAGE_STATES = {STAGE_NOT_RUN, STAGE_SUCCESS, STAGE_FAILED}


def _stage_state(state: Optional[str], success: bool) -> str:
    resolved = state or (STAGE_SUCCESS if success else STAGE_FAILED)
    if resolved not in STAGE_STATES:
        raise ValueError(f"invalid stage state: {resolved}")
    return resolved


def _json(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_json(item) for item in value]
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class FramePacket:
    frame_id: int
    captured_at: float
    frame: Any
    source: str = "camera"
    captured_monotonic: Optional[float] = None


@dataclass(frozen=True)
class LabelCandidate:
    bbox: tuple[float, float, float, float]
    confidence: float = 1.0
    detector: str = "unknown"
    frame_id: Optional[int] = None
    corners: Optional[tuple[tuple[float, float], ...]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bbox": list(self.bbox),
            "confidence": self.confidence,
            "detector": self.detector,
            "frame_id": self.frame_id,
            "corners": _json(self.corners),
        }


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float
    polygon: Optional[list[list[float]]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "polygon": self.polygon,
            "bbox": self.polygon,
        }


@dataclass
class RawOCRResult:
    engine: str
    lines: list[OCRLine] = field(default_factory=list)
    elapsed_ms: float = 0.0
    success: bool = True
    state: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw: Optional[dict[str, Any]] = None
    backend: Optional[str] = None
    device: Optional[str] = None
    model: Optional[str] = None

    def __post_init__(self) -> None:
        self.state = _stage_state(self.state, self.success)
        self.success = self.state == STAGE_SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "backend": self.backend,
            "device": self.device,
            "model": self.model,
            "lines": [_json(line) for line in self.lines],
            "elapsed_ms": self.elapsed_ms,
            "success": self.success,
            "state": self.state,
            "status": self.state,
            "error": self.error,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "raw": _json(self.raw),
        }


@dataclass(frozen=True)
class BarcodeResult:
    value: Optional[str]
    format: Optional[str] = None
    confidence: Optional[float] = None
    valid: Optional[bool] = None
    position: Optional[Any] = None
    error: Optional[str] = None
    success: bool = True
    state: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        resolved = _stage_state(self.state, self.success)
        object.__setattr__(self, "state", resolved)
        object.__setattr__(self, "success", resolved == STAGE_SUCCESS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "format": self.format,
            "confidence": self.confidence,
            "valid": self.valid,
            "position": _json(self.position),
            "error": self.error,
            "success": self.success,
            "state": self.state,
            "status": self.state,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class ExtractedField:
    value: Optional[str]
    confidence: float = 0.0
    source: Optional[str] = None
    line_text: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceItem:
    """Profile-independent observation preserved before semantic mapping.

    ``text`` is source evidence, not a canonical business-field value.  The
    optional metadata is deliberately descriptive (for example barcode
    format/validity) and must not be interpreted as a field mapping.
    """

    kind: str
    text: Optional[str]
    confidence: Optional[float]
    source: str
    polygon: Optional[Any] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, Mapping):
            raise TypeError("evidence metadata must be an object")
        object.__setattr__(self, "polygon", freeze_json(self.polygon))
        object.__setattr__(self, "metadata", freeze_json(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "confidence": self.confidence,
            "source": self.source,
            "polygon": thaw_json(self.polygon),
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class QualityReport:
    status: str = "PASS"
    state: Optional[str] = None
    sharpness: Optional[float] = None
    brightness: Optional[float] = None
    underexposed_ratio: Optional[float] = None
    overexposed_ratio: Optional[float] = None
    glare_ratio: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    area: Optional[int] = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        default_success = self.status != "ERROR"
        object.__setattr__(self, "state", _stage_state(self.state, default_success))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["brightness_mean"] = self.brightness
        data["passed"] = self.status == "PASS" if self.state == STAGE_SUCCESS else None
        data["reasons"] = list(self.reasons)
        return data


@dataclass(frozen=True)
class LabelCandidateScore:
    total: float
    detection_confidence: float
    crop_sharpness: float
    crop_exposure: float
    crop_glare: float
    label_area_ratio: float
    frame_freshness: float
    crop_validity: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    status: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons)}


@dataclass
class InspectionResult:
    event_id: str
    camera_id: str
    frame_id: Optional[int] = None
    frame_timestamp: Optional[float] = None
    label: Optional[LabelCandidate] = None
    crop_bbox: Optional[tuple[float, float, float, float]] = None
    candidate_score: Optional[LabelCandidateScore] = None
    raw_ocr: RawOCRResult = field(
        default_factory=lambda: RawOCRResult(
            engine="none", success=False, state=STAGE_NOT_RUN
        )
    )
    extracted: dict[str, ExtractedField] = field(default_factory=dict)
    barcode: BarcodeResult = field(
        default_factory=lambda: BarcodeResult(
            value=None, success=False, state=STAGE_NOT_RUN
        )
    )
    barcodes: list[BarcodeResult] = field(default_factory=list)
    quality: QualityReport = field(
        default_factory=lambda: QualityReport(status="NOT_RUN", state=STAGE_NOT_RUN)
    )
    validation: ValidationResult = field(
        default_factory=lambda: ValidationResult(status="ERROR", reasons=("NOT_RUN",))
    )
    timing: dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None
    evidence: list[EvidenceItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "camera_id": self.camera_id,
            "frame_id": self.frame_id,
            "frame_timestamp": self.frame_timestamp,
            "label": _json(self.label),
            "crop_bbox": _json(self.crop_bbox),
            "candidate_score": _json(self.candidate_score),
            "raw_ocr": _json(self.raw_ocr),
            "evidence": [_json(item) for item in self.evidence],
            "extracted": {key: _json(value) for key, value in self.extracted.items()},
            "barcode": _json(self.barcode),
            "barcodes": [_json(item) for item in self.barcodes],
            "quality": _json(self.quality),
            "validation": _json(self.validation),
            "timing": dict(self.timing),
            "error": self.error,
        }
