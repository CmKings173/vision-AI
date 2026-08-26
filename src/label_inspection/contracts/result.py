"""Versioned terminal inspection result contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .core import (
    BusinessStatus,
    ContractValidationError,
    InspectionError,
    ProcessingStatus,
    freeze_json,
    require_enum,
    require_epoch_ms,
    require_fields,
    require_text,
    require_uuid,
    thaw_json,
)

RESULT_SCHEMA_VERSION = "inspection-result.v1"


@dataclass(frozen=True)
class InspectionResult:
    event_id: str
    trigger_id: str
    station_id: str
    camera_id: str
    processing_status: ProcessingStatus
    business_status: BusinessStatus | None
    inference_executed: bool
    created_at_ms: int
    completed_at_ms: int
    reasons: tuple[str, ...] = ()
    quality: Mapping[str, Any] = field(default_factory=dict)
    result_payload: Mapping[str, Any] = field(default_factory=dict)
    error: InspectionError | None = None
    schema_version: str = RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESULT_SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported schema_version: {self.schema_version!r}"
            )
        processing = require_enum(
            self.processing_status, ProcessingStatus, "processing_status"
        )
        business = (
            None
            if self.business_status is None
            else require_enum(self.business_status, BusinessStatus, "business_status")
        )
        if not isinstance(self.inference_executed, bool):
            raise ContractValidationError("inference_executed must be boolean")
        if processing not in {ProcessingStatus.COMPLETED, ProcessingStatus.ERROR}:
            raise ContractValidationError(
                "InspectionResult processing_status must be terminal"
            )
        if processing is ProcessingStatus.ERROR:
            if business is not None:
                raise ContractValidationError("technical ERROR must have null business_status")
            if self.error is None:
                raise ContractValidationError("technical ERROR requires error")
        elif self.error is not None:
            raise ContractValidationError("non-ERROR result cannot contain technical error")
        if processing is ProcessingStatus.COMPLETED and business is None:
            raise ContractValidationError(
                "COMPLETED result requires a business_status"
            )
        if business is not None and processing is not ProcessingStatus.COMPLETED:
            raise ContractValidationError(
                "business_status is only valid for a COMPLETED result"
            )

        object.__setattr__(self, "event_id", require_uuid(self.event_id, "event_id"))
        object.__setattr__(self, "trigger_id", require_uuid(self.trigger_id, "trigger_id"))
        object.__setattr__(self, "station_id", require_text(self.station_id, "station_id"))
        object.__setattr__(self, "camera_id", require_text(self.camera_id, "camera_id"))
        object.__setattr__(
            self, "created_at_ms", require_epoch_ms(self.created_at_ms, "created_at_ms")
        )
        object.__setattr__(
            self,
            "completed_at_ms",
            require_epoch_ms(self.completed_at_ms, "completed_at_ms"),
        )
        if self.completed_at_ms < self.created_at_ms:
            raise ContractValidationError("completed_at_ms cannot precede created_at_ms")
        object.__setattr__(self, "processing_status", processing)
        object.__setattr__(self, "business_status", business)
        object.__setattr__(
            self,
            "reasons",
            tuple(require_text(reason, "reason") for reason in self.reasons),
        )
        object.__setattr__(self, "quality", freeze_json(self.quality))
        object.__setattr__(self, "result_payload", freeze_json(self.result_payload))

    @classmethod
    def quality_rejected(
        cls,
        *,
        event_id: str,
        trigger_id: str,
        station_id: str,
        camera_id: str,
        created_at_ms: int,
        completed_at_ms: int,
        quality: Mapping[str, Any],
        reasons: tuple[str, ...] = ("QUALITY_REJECTED",),
    ) -> InspectionResult:
        normalized_reasons = tuple(reasons)
        if "QUALITY_REJECTED" not in normalized_reasons:
            normalized_reasons = ("QUALITY_REJECTED", *normalized_reasons)
        return cls(
            event_id=event_id,
            trigger_id=trigger_id,
            station_id=station_id,
            camera_id=camera_id,
            processing_status=ProcessingStatus.COMPLETED,
            business_status=BusinessStatus.REVIEW,
            inference_executed=False,
            created_at_ms=created_at_ms,
            completed_at_ms=completed_at_ms,
            reasons=normalized_reasons,
            quality=quality,
        )

    @classmethod
    def preparation_error(
        cls,
        *,
        event_id: str,
        trigger_id: str,
        station_id: str,
        camera_id: str,
        created_at_ms: int,
        completed_at_ms: int,
        error: InspectionError,
        reasons: tuple[str, ...] = (),
    ) -> InspectionResult:
        return cls(
            event_id=event_id,
            trigger_id=trigger_id,
            station_id=station_id,
            camera_id=camera_id,
            processing_status=ProcessingStatus.ERROR,
            business_status=None,
            inference_executed=False,
            created_at_ms=created_at_ms,
            completed_at_ms=completed_at_ms,
            reasons=reasons or (error.code,),
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "trigger_id": self.trigger_id,
            "station_id": self.station_id,
            "camera_id": self.camera_id,
            "processing_status": self.processing_status.value,
            "business_status": (
                None if self.business_status is None else self.business_status.value
            ),
            "inference_executed": self.inference_executed,
            "created_at_ms": self.created_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "reasons": list(self.reasons),
            "quality": thaw_json(self.quality),
            "result_payload": thaw_json(self.result_payload),
            "error": None if self.error is None else self.error.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InspectionResult:
        allowed = {
            "schema_version",
            "event_id",
            "trigger_id",
            "station_id",
            "camera_id",
            "processing_status",
            "business_status",
            "inference_executed",
            "created_at_ms",
            "completed_at_ms",
            "reasons",
            "quality",
            "result_payload",
            "error",
        }
        require_fields(
            payload,
            allowed=allowed,
            required=allowed,
            contract_name="InspectionResult",
        )
        error_payload = payload["error"]
        if error_payload is not None and not isinstance(error_payload, Mapping):
            raise ContractValidationError("error must be an object or null")
        reasons = payload["reasons"]
        if not isinstance(reasons, (list, tuple)):
            raise ContractValidationError("reasons must be an array")
        return cls(
            schema_version=payload["schema_version"],
            event_id=payload["event_id"],
            trigger_id=payload["trigger_id"],
            station_id=payload["station_id"],
            camera_id=payload["camera_id"],
            processing_status=payload["processing_status"],
            business_status=payload["business_status"],
            inference_executed=payload["inference_executed"],
            created_at_ms=payload["created_at_ms"],
            completed_at_ms=payload["completed_at_ms"],
            reasons=tuple(reasons),
            quality=payload["quality"],
            result_payload=payload["result_payload"],
            error=None if error_payload is None else InspectionError.from_dict(error_payload),
        )
