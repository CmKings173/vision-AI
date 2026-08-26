"""Canonical station/date/event object-key layout."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from ..contracts import ArtifactRef, InspectionJob
from ..contracts.core import require_epoch_ms, require_text, require_uuid
from .base import ArtifactPolicyError

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class EventObjectKeys:
    prefix: str
    selected_frame: str
    label_crop: str
    job: str
    result: str


@dataclass(frozen=True)
class ArtifactKeyPolicy:
    """Worker-side allowlist for untrusted Phase 2 artifact references."""

    bucket: str
    max_label_crop_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "bucket", require_text(self.bucket, "bucket"))
        if (
            isinstance(self.max_label_crop_bytes, bool)
            or not isinstance(self.max_label_crop_bytes, int)
            or self.max_label_crop_bytes < 1
        ):
            raise ValueError("max_label_crop_bytes must be a positive integer")

    def validate_job(self, job: InspectionJob) -> None:
        if set(job.artifacts) != {"selected_frame", "label_crop"}:
            raise ArtifactPolicyError(
                "Job artifact set is not allowed by the Phase 2 worker policy."
            )
        keys = event_object_keys(
            station_id=job.station_id,
            event_id=job.event_id,
            occurred_at_ms=job.triggered_at_ms,
        )
        self._validate_reference(
            job.artifacts["selected_frame"],
            expected_key=keys.selected_frame,
            expected_content_type="image/jpeg",
            max_bytes=None,
        )
        self._validate_reference(
            job.artifacts["label_crop"],
            expected_key=keys.label_crop,
            expected_content_type="image/png",
            max_bytes=self.max_label_crop_bytes,
        )

    def result_reference(self, job: InspectionJob, content: bytes) -> ArtifactRef:
        key = event_object_keys(
            station_id=job.station_id,
            event_id=job.event_id,
            occurred_at_ms=job.triggered_at_ms,
        ).result
        return ArtifactRef(
            bucket=self.bucket,
            key=key,
            sha256=hashlib.sha256(content).hexdigest(),
            content_type="application/json",
            size_bytes=len(content),
        )

    def result_location(self, job: InspectionJob) -> tuple[str, str]:
        key = event_object_keys(
            station_id=job.station_id,
            event_id=job.event_id,
            occurred_at_ms=job.triggered_at_ms,
        ).result
        return self.bucket, key

    def _validate_reference(
        self,
        reference: ArtifactRef,
        *,
        expected_key: str,
        expected_content_type: str,
        max_bytes: int | None,
    ) -> None:
        if reference.bucket != self.bucket:
            raise ArtifactPolicyError("Artifact bucket is not allowed.")
        if reference.key != expected_key:
            raise ArtifactPolicyError("Artifact key is outside the expected event namespace.")
        if reference.content_type != expected_content_type:
            raise ArtifactPolicyError("Artifact content type is not allowed.")
        if (
            reference.size_bytes is None
            or reference.size_bytes < 1
            or (max_bytes is not None and reference.size_bytes > max_bytes)
        ):
            raise ArtifactPolicyError("Artifact declared size is not allowed.")
        if not re.fullmatch(r"[0-9a-f]{64}", reference.sha256):
            raise ArtifactPolicyError("Artifact checksum format is invalid.")


def event_object_keys(
    *, station_id: str, event_id: str, occurred_at_ms: int
) -> EventObjectKeys:
    if not isinstance(station_id, str) or not _SAFE_SEGMENT.fullmatch(station_id):
        raise ValueError("station_id is not safe for an object key")
    canonical_event_id = require_uuid(event_id, "event_id")
    timestamp_ms = require_epoch_ms(occurred_at_ms, "occurred_at_ms")
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    prefix = f"{station_id}/{timestamp:%Y/%m/%d}/{canonical_event_id}"
    return EventObjectKeys(
        prefix=prefix,
        selected_frame=f"{prefix}/source/selected_frame.jpg",
        label_crop=f"{prefix}/source/label_crop.png",
        job=f"{prefix}/metadata/job.json",
        result=f"{prefix}/result/result.json",
    )
