"""Shared Phase 2 contract primitives and boundary validation."""

from __future__ import annotations

import math
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class ContractValidationError(ValueError):
    """Raised when untrusted cross-process payload data is invalid."""


class ProcessingStatus(str, Enum):
    CREATED = "CREATED"
    CAPTURED = "CAPTURED"
    PREPARED = "PREPARED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class BusinessStatus(str, Enum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class DeliveryStatus(str, Enum):
    LOCAL_ONLY = "LOCAL_ONLY"
    ARTIFACTS_READY = "ARTIFACTS_READY"
    JOB_PUBLISHED = "JOB_PUBLISHED"
    TERMINAL_RESULT_DURABLE = "TERMINAL_RESULT_DURABLE"


PROCESSING_STATUS_OWNERS = MappingProxyType(
    {
        ProcessingStatus.CREATED: "station",
        ProcessingStatus.CAPTURED: "station",
        ProcessingStatus.PREPARED: "station",
        ProcessingStatus.QUEUED: "confirmed_publisher",
        ProcessingStatus.PROCESSING: "worker",
        ProcessingStatus.COMPLETED: "station_or_worker",
        ProcessingStatus.ERROR: "station_or_worker",
    }
)
DELIVERY_STATUS_OWNER = "spool_dispatcher"
BUSINESS_STATUS_OWNER = "validator_or_station_quality_gate"


def epoch_ms_now() -> int:
    """Return a persistent wall-clock timestamp in Unix Epoch milliseconds."""

    return time.time_ns() // 1_000_000


def new_uuid() -> str:
    """Return a canonical UUIDv4 string for distributed identity."""

    return str(uuid.uuid4())


def require_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{field_name} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ContractValidationError(f"{field_name} must be a valid UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ContractValidationError(f"{field_name} must be a canonical UUID")
    return canonical


def require_epoch_ms(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(
            f"{field_name} must be a non-negative integer Unix Epoch millisecond"
        )
    return value


def require_text(value: Any, field_name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field_name} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ContractValidationError(f"{field_name} exceeds {max_length} characters")
    return normalized


def require_fields(
    payload: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    contract_name: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise ContractValidationError(f"{contract_name} must be an object")
    unknown = set(payload) - allowed
    if unknown:
        raise ContractValidationError(
            f"{contract_name} contains unknown fields: {sorted(unknown)}"
        )
    missing = required - set(payload)
    if missing:
        raise ContractValidationError(
            f"{contract_name} is missing required fields: {sorted(missing)}"
        )


def require_enum(value: Any, enum_type: type[Enum], field_name: str):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (ValueError, TypeError) as exc:
        raise ContractValidationError(f"invalid {field_name}: {value!r}") from exc


def freeze_json(value: Any) -> Any:
    """Copy JSON-like metadata into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(
                "contract metadata numbers must be finite JSON values"
            )
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ContractValidationError(
        f"contract metadata must be JSON-compatible, got {type(value).__name__}"
    )


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


@dataclass(frozen=True)
class TriggerEvent:
    event_id: str
    trigger_id: str
    station_id: str
    camera_id: str
    triggered_at_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", require_uuid(self.event_id, "event_id"))
        object.__setattr__(self, "trigger_id", require_uuid(self.trigger_id, "trigger_id"))
        object.__setattr__(self, "station_id", require_text(self.station_id, "station_id"))
        object.__setattr__(self, "camera_id", require_text(self.camera_id, "camera_id"))
        object.__setattr__(
            self,
            "triggered_at_ms",
            require_epoch_ms(self.triggered_at_ms, "triggered_at_ms"),
        )

    @classmethod
    def create(
        cls,
        *,
        station_id: str,
        camera_id: str,
        triggered_at_ms: int | None = None,
    ) -> TriggerEvent:
        return cls(
            event_id=new_uuid(),
            trigger_id=new_uuid(),
            station_id=station_id,
            camera_id=camera_id,
            triggered_at_ms=epoch_ms_now() if triggered_at_ms is None else triggered_at_ms,
        )


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ArtifactRef:
    bucket: str
    key: str
    sha256: str
    content_type: str
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        bucket = require_text(self.bucket, "bucket")
        key = require_text(self.key, "key", max_length=1024)
        if key.startswith(("/", "\\")) or "\\" in key or ".." in key.split("/"):
            raise ContractValidationError("key must be a relative object key without traversal")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise ContractValidationError("sha256 must contain exactly 64 hexadecimal characters")
        content_type = require_text(self.content_type, "content_type")
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ContractValidationError("size_bytes must be a non-negative integer or null")
        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "sha256", self.sha256.lower())
        object.__setattr__(self, "content_type", content_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArtifactRef:
        allowed = {"bucket", "key", "sha256", "content_type", "size_bytes"}
        require_fields(
            payload,
            allowed=allowed,
            required={"bucket", "key", "sha256", "content_type"},
            contract_name="ArtifactRef",
        )
        return cls(
            bucket=payload["bucket"],
            key=payload["key"],
            sha256=payload["sha256"],
            content_type=payload["content_type"],
            size_bytes=payload.get("size_bytes"),
        )


@dataclass(frozen=True)
class InspectionError:
    code: str
    stage: str
    message: str
    retryable: bool
    attempt: int = 0
    safe_details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_text(self.code, "error.code"))
        object.__setattr__(self, "stage", require_text(self.stage, "error.stage"))
        object.__setattr__(self, "message", require_text(self.message, "error.message", max_length=2048))
        if not isinstance(self.retryable, bool):
            raise ContractValidationError("error.retryable must be boolean")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ContractValidationError("error.attempt must be a non-negative integer")
        object.__setattr__(self, "safe_details", freeze_json(self.safe_details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "retryable": self.retryable,
            "attempt": self.attempt,
            "safe_details": thaw_json(self.safe_details),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InspectionError:
        allowed = {"code", "stage", "message", "retryable", "attempt", "safe_details"}
        require_fields(
            payload,
            allowed=allowed,
            required={"code", "stage", "message", "retryable"},
            contract_name="InspectionError",
        )
        return cls(
            code=payload["code"],
            stage=payload["stage"],
            message=payload["message"],
            retryable=payload["retryable"],
            attempt=payload.get("attempt", 0),
            safe_details=payload.get("safe_details", {}),
        )
