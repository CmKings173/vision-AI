"""Immutable, versioned inference job contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .core import (
    ArtifactRef,
    ContractValidationError,
    ProcessingStatus,
    freeze_json,
    require_enum,
    require_epoch_ms,
    require_fields,
    require_text,
    require_uuid,
    thaw_json,
)

JOB_SCHEMA_VERSION = "inspection-job.v1"


@dataclass(frozen=True)
class InspectionJob:
    event_id: str
    trigger_id: str
    station_id: str
    camera_id: str
    triggered_at_ms: int
    received_at_ms: int
    prepared_at_ms: int
    created_at_ms: int
    artifacts: Mapping[str, ArtifactRef]
    source_timestamp_ms: int | None = None
    selection: Mapping[str, Any] = field(default_factory=dict)
    locator: Mapping[str, Any] = field(default_factory=dict)
    quality: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = JOB_SCHEMA_VERSION
    processing_status: ProcessingStatus = ProcessingStatus.PREPARED

    def __post_init__(self) -> None:
        if self.schema_version != JOB_SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported schema_version: {self.schema_version!r}"
            )
        status = require_enum(self.processing_status, ProcessingStatus, "processing_status")
        if status is not ProcessingStatus.PREPARED:
            raise ContractValidationError("InspectionJob processing_status must be PREPARED")

        object.__setattr__(self, "event_id", require_uuid(self.event_id, "event_id"))
        object.__setattr__(self, "trigger_id", require_uuid(self.trigger_id, "trigger_id"))
        object.__setattr__(self, "station_id", require_text(self.station_id, "station_id"))
        object.__setattr__(self, "camera_id", require_text(self.camera_id, "camera_id"))
        for name in ("triggered_at_ms", "received_at_ms", "prepared_at_ms", "created_at_ms"):
            object.__setattr__(self, name, require_epoch_ms(getattr(self, name), name))
        if self.source_timestamp_ms is not None:
            object.__setattr__(
                self,
                "source_timestamp_ms",
                require_epoch_ms(self.source_timestamp_ms, "source_timestamp_ms"),
            )
        if self.prepared_at_ms < max(self.triggered_at_ms, self.received_at_ms):
            raise ContractValidationError(
                "prepared_at_ms cannot precede trigger or selected-frame receipt"
            )
        if self.created_at_ms < self.prepared_at_ms:
            raise ContractValidationError("created_at_ms cannot precede prepared_at_ms")

        if not isinstance(self.artifacts, Mapping) or "label_crop" not in self.artifacts:
            raise ContractValidationError("artifacts must include label_crop")
        artifact_copy: dict[str, ArtifactRef] = {}
        for name, value in self.artifacts.items():
            artifact_name = require_text(name, "artifact name")
            artifact_copy[artifact_name] = (
                value if isinstance(value, ArtifactRef) else ArtifactRef.from_dict(value)
            )
        object.__setattr__(self, "artifacts", MappingProxyType(artifact_copy))
        object.__setattr__(self, "selection", freeze_json(self.selection))
        object.__setattr__(self, "locator", freeze_json(self.locator))
        object.__setattr__(self, "quality", freeze_json(self.quality))
        object.__setattr__(self, "provenance", freeze_json(self.provenance))
        object.__setattr__(self, "processing_status", status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "trigger_id": self.trigger_id,
            "station_id": self.station_id,
            "camera_id": self.camera_id,
            "triggered_at_ms": self.triggered_at_ms,
            "received_at_ms": self.received_at_ms,
            "source_timestamp_ms": self.source_timestamp_ms,
            "prepared_at_ms": self.prepared_at_ms,
            "created_at_ms": self.created_at_ms,
            "processing_status": self.processing_status.value,
            "selection": thaw_json(self.selection),
            "locator": thaw_json(self.locator),
            "quality": thaw_json(self.quality),
            "artifacts": {name: ref.to_dict() for name, ref in self.artifacts.items()},
            "provenance": thaw_json(self.provenance),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InspectionJob:
        allowed = {
            "schema_version",
            "event_id",
            "trigger_id",
            "station_id",
            "camera_id",
            "triggered_at_ms",
            "received_at_ms",
            "source_timestamp_ms",
            "prepared_at_ms",
            "created_at_ms",
            "processing_status",
            "selection",
            "locator",
            "quality",
            "artifacts",
            "provenance",
        }
        require_fields(
            payload,
            allowed=allowed,
            required=allowed,
            contract_name="InspectionJob",
        )
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, Mapping):
            raise ContractValidationError("artifacts must be an object")
        return cls(
            schema_version=payload["schema_version"],
            event_id=payload["event_id"],
            trigger_id=payload["trigger_id"],
            station_id=payload["station_id"],
            camera_id=payload["camera_id"],
            triggered_at_ms=payload["triggered_at_ms"],
            received_at_ms=payload["received_at_ms"],
            source_timestamp_ms=payload["source_timestamp_ms"],
            prepared_at_ms=payload["prepared_at_ms"],
            created_at_ms=payload["created_at_ms"],
            processing_status=payload["processing_status"],
            selection=payload["selection"],
            locator=payload["locator"],
            quality=payload["quality"],
            artifacts={name: ArtifactRef.from_dict(value) for name, value in artifacts.items()},
            provenance=payload["provenance"],
        )
