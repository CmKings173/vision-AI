"""Typed local-spool state, records, recovery reports, and safe errors."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..contracts import DeliveryStatus, InspectionError, InspectionJob, InspectionResult
from ..contracts.core import require_epoch_ms, require_text, require_uuid

SPOOL_STATE_SCHEMA_VERSION = "local-spool-state.v1"


class RecordType(str, Enum):
    """Kinds of immutable records stored in the local outbox."""

    INFERENCE_JOB = "INFERENCE_JOB"
    TERMINAL_RESULT = "TERMINAL_RESULT"


class SpoolError(RuntimeError):
    """Base exception with a safe cross-boundary error representation."""

    code = "SPOOL_ERROR"
    retryable = True

    def to_inspection_error(self) -> InspectionError:
        return InspectionError(
            code=self.code,
            stage="LOCAL_SPOOL",
            message=str(self),
            retryable=self.retryable,
            attempt=0,
        )


class SpoolPathError(SpoolError):
    code = "SPOOL_PATH_ERROR"
    retryable = False


class SpoolConflictError(SpoolError):
    code = "SPOOL_CONFLICT"
    retryable = False


class SpoolCommitError(SpoolError):
    code = "SPOOL_COMMIT_ERROR"
    retryable = True


class SpoolStateError(SpoolError):
    code = "SPOOL_STATE_ERROR"
    retryable = False


class SpoolCorruptionError(SpoolError):
    code = "SPOOL_CORRUPT"
    retryable = False

    def __init__(self, message: str, *, corruption_code: str) -> None:
        super().__init__(message)
        self.corruption_code = corruption_code


@dataclass(frozen=True)
class SpoolLimits:
    max_pending_events: int
    max_pending_bytes: int
    min_free_disk_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_pending_events, bool)
            or not isinstance(self.max_pending_events, int)
            or self.max_pending_events < 1
        ):
            raise ValueError("max_pending_events must be >= 1")
        if (
            isinstance(self.max_pending_bytes, bool)
            or not isinstance(self.max_pending_bytes, int)
            or self.max_pending_bytes < 1
        ):
            raise ValueError("max_pending_bytes must be >= 1")
        if (
            isinstance(self.min_free_disk_bytes, bool)
            or not isinstance(self.min_free_disk_bytes, int)
            or self.min_free_disk_bytes < 0
        ):
            raise ValueError("min_free_disk_bytes must be >= 0")


@dataclass(frozen=True)
class SpoolUsage:
    pending_events: int
    pending_bytes: int
    free_disk_bytes: int | None


class SpoolCapacityError(SpoolError):
    retryable = True

    def __init__(self, reason: str, message: str, *, usage: SpoolUsage) -> None:
        super().__init__(message)
        self.reason = require_text(reason, "capacity reason")
        self.code = self.reason
        self.usage = usage


@dataclass(frozen=True)
class FileManifest:
    sha256: str
    size_bytes: int
    content_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ):
            raise ValueError("file manifest sha256 must be lowercase hexadecimal")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("file manifest size_bytes must be non-negative")
        object.__setattr__(
            self, "content_type", require_text(self.content_type, "content_type")
        )

    @classmethod
    def from_bytes(cls, content: bytes, *, content_type: str) -> FileManifest:
        return cls(
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type=content_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> FileManifest:
        required = {"sha256", "size_bytes", "content_type"}
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ValueError("invalid file manifest fields")
        return cls(
            sha256=payload["sha256"],
            size_bytes=payload["size_bytes"],
            content_type=payload["content_type"],
        )


@dataclass(frozen=True)
class SpoolState:
    event_id: str
    record_type: RecordType
    delivery_status: DeliveryStatus
    created_at_ms: int
    updated_at_ms: int
    files: Mapping[str, FileManifest] = field(default_factory=dict)
    schema_version: str = SPOOL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SPOOL_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported local spool state schema")
        object.__setattr__(self, "event_id", require_uuid(self.event_id, "event_id"))
        record_type = RecordType(self.record_type)
        delivery_status = DeliveryStatus(self.delivery_status)
        if (
            delivery_status is DeliveryStatus.JOB_PUBLISHED
            and record_type is not RecordType.INFERENCE_JOB
        ) or (
            delivery_status is DeliveryStatus.TERMINAL_RESULT_DURABLE
            and record_type is not RecordType.TERMINAL_RESULT
        ):
            raise ValueError("delivery status is incompatible with record type")
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "delivery_status", delivery_status)
        created = require_epoch_ms(self.created_at_ms, "created_at_ms")
        updated = require_epoch_ms(self.updated_at_ms, "updated_at_ms")
        if updated < created:
            raise ValueError("updated_at_ms cannot precede created_at_ms")
        if not isinstance(self.files, Mapping):
            raise TypeError("local spool state files must be an object")
        manifests: dict[str, FileManifest] = {}
        for name, manifest in self.files.items():
            safe_name = require_text(name, "manifest file name")
            if (
                safe_name in {".", ".."}
                or "/" in safe_name
                or "\\" in safe_name
            ):
                raise ValueError("manifest file name must not contain a path")
            if not isinstance(manifest, FileManifest):
                raise TypeError("manifest value must be a FileManifest")
            manifests[safe_name] = manifest
        object.__setattr__(self, "files", MappingProxyType(manifests))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "record_type": self.record_type.value,
            "delivery_status": self.delivery_status.value,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "files": {
                name: manifest.to_dict() for name, manifest in self.files.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SpoolState:
        required = {
            "schema_version",
            "event_id",
            "record_type",
            "delivery_status",
            "created_at_ms",
            "updated_at_ms",
            "files",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise ValueError("invalid local spool state fields")
        files = payload["files"]
        if not isinstance(files, Mapping):
            raise TypeError("local spool state files must be an object")
        record_type = RecordType(payload["record_type"])
        delivery_status = payload["delivery_status"]
        # Read-only migration for Phase 2 records written before delivery end
        # states were split. New writes never emit the overloaded value.
        if delivery_status == "PUBLISHED":
            delivery_status = (
                DeliveryStatus.JOB_PUBLISHED
                if record_type is RecordType.INFERENCE_JOB
                else DeliveryStatus.TERMINAL_RESULT_DURABLE
            )
        return cls(
            schema_version=payload["schema_version"],
            event_id=payload["event_id"],
            record_type=record_type,
            delivery_status=delivery_status,
            created_at_ms=payload["created_at_ms"],
            updated_at_ms=payload["updated_at_ms"],
            files={name: FileManifest.from_dict(value) for name, value in files.items()},
        )


@dataclass(frozen=True)
class SpoolRecord:
    path: Path
    state: SpoolState
    job: InspectionJob | None = None
    result: InspectionResult | None = None

    @property
    def record_type(self) -> RecordType:
        return self.state.record_type

    def frozen_job_bytes(self) -> bytes:
        if self.job is None:
            raise ValueError("spool record does not contain an inference job")
        return (self.path / "job.json").read_bytes()


@dataclass(frozen=True)
class RecoveryIssue:
    event_id: str
    code: str
    message: str
    path: Path


@dataclass(frozen=True)
class RecoveryReport:
    pending_records: tuple[SpoolRecord, ...] = ()
    delivered_records: tuple[SpoolRecord, ...] = ()
    corrupt_records: tuple[RecoveryIssue, ...] = ()
    incomplete_paths: tuple[Path, ...] = ()
