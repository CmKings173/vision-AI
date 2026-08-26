"""Durable station-side outbox for prepared inspection events.

An event becomes dispatchable only after its complete temporary directory is
atomically renamed to the canonical event directory.  The serialized job is
written once and subsequently treated as immutable.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..contracts import (
    ArtifactRef,
    ContractValidationError,
    DeliveryStatus,
    InspectionJob,
    InspectionResult,
    ProcessingStatus,
    epoch_ms_now,
)
from ..contracts.core import require_text, require_uuid
from ..storage.keys import event_object_keys
from .preparation import PreparationOutcome
from .spool_models import (
    FileManifest,
    RecordType,
    RecoveryIssue,
    RecoveryReport,
    SpoolCapacityError,
    SpoolCommitError,
    SpoolConflictError,
    SpoolCorruptionError,
    SpoolError,
    SpoolLimits,
    SpoolPathError,
    SpoolRecord,
    SpoolState,
    SpoolStateError,
    SpoolUsage,
)

# Linux/POSIX production requires directory fsync for rename durability. Windows
# development filesystems do not consistently permit opening directories as
# file descriptors, so that platform has an explicit best-effort policy.
_DIRECTORY_FSYNC_REQUIRED = os.name == "posix"


class LocalSpool:
    """Filesystem-backed durable outbox with event-directory atomicity."""

    def __init__(
        self,
        root: Path | str,
        *,
        bucket: str = "vision-inspections",
        limits: SpoolLimits | None = None,
    ) -> None:
        requested_root = Path(root)
        requested_root.mkdir(parents=True, exist_ok=True)
        self.root = requested_root.resolve()
        self.bucket = require_text(bucket, "bucket")
        self.limits = limits

    def event_path(self, event_id: str) -> Path:
        try:
            canonical_id = require_uuid(event_id, "event_id")
        except ContractValidationError as exc:
            raise SpoolPathError("event_id is not a canonical UUID") from exc
        return self._contained_child(canonical_id)

    def check_capacity(
        self, *, estimated_bytes: int = 0, reserve_events: int = 1
    ) -> SpoolUsage:
        """Fail closed when a prospective trigger would exceed local limits."""

        if (
            isinstance(estimated_bytes, bool)
            or not isinstance(estimated_bytes, int)
            or estimated_bytes < 0
        ):
            raise ValueError("estimated_bytes must be a non-negative integer")
        if (
            isinstance(reserve_events, bool)
            or not isinstance(reserve_events, int)
            or reserve_events < 0
        ):
            raise ValueError("reserve_events must be a non-negative integer")
        usage = self._measure_usage()
        if self.limits is None:
            return usage
        if usage.pending_events + reserve_events > self.limits.max_pending_events:
            raise SpoolCapacityError(
                "SPOOL_MAX_PENDING_EVENTS",
                "Local spool pending-event capacity is exhausted.",
                usage=usage,
            )
        if usage.pending_bytes + estimated_bytes > self.limits.max_pending_bytes:
            raise SpoolCapacityError(
                "SPOOL_MAX_PENDING_BYTES",
                "Local spool pending-byte capacity is exhausted.",
                usage=usage,
            )
        assert usage.free_disk_bytes is not None
        if (
            usage.free_disk_bytes - estimated_bytes
            < self.limits.min_free_disk_bytes
        ):
            raise SpoolCapacityError(
                "SPOOL_MIN_FREE_DISK",
                "Local spool minimum free-disk reserve would be violated.",
                usage=usage,
            )
        return usage

    def commit_outcome(
        self,
        outcome: PreparationOutcome,
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> SpoolRecord:
        """Persist one inference or station-terminal outcome atomically."""

        if not outcome.inference_required:
            return self._commit_terminal_outcome(outcome, provenance=provenance)

        if (
            outcome.processing_status is not ProcessingStatus.PREPARED
            or outcome.prepared is None
        ):
            raise SpoolCommitError(
                "Local spool inference commit requires a prepared outcome."
            )

        event_id = outcome.event_id
        final_path = self.event_path(event_id)
        temp_path = self._contained_child(f".tmp_{event_id}")
        if self._lexists(final_path) or self._lexists(temp_path):
            raise SpoolConflictError(
                "Local spool already contains this event or an incomplete commit."
            )

        prepared = outcome.prepared
        try:
            selected_bytes = self._encode_image(
                prepared.selected_frame, extension=".jpg"
            )
            crop_bytes = self._encode_image(prepared.label_crop, extension=".png")
            selected_manifest = FileManifest.from_bytes(
                selected_bytes, content_type="image/jpeg"
            )
            crop_manifest = FileManifest.from_bytes(
                crop_bytes, content_type="image/png"
            )
            object_keys = event_object_keys(
                station_id=prepared.station_id,
                event_id=event_id,
                occurred_at_ms=prepared.triggered_at_ms,
            )
            artifacts = {
                "selected_frame": ArtifactRef(
                    bucket=self.bucket,
                    key=object_keys.selected_frame,
                    sha256=selected_manifest.sha256,
                    content_type=selected_manifest.content_type,
                    size_bytes=selected_manifest.size_bytes,
                ),
                "label_crop": ArtifactRef(
                    bucket=self.bucket,
                    key=object_keys.label_crop,
                    sha256=crop_manifest.sha256,
                    content_type=crop_manifest.content_type,
                    size_bytes=crop_manifest.size_bytes,
                ),
            }
            created_at_ms = max(epoch_ms_now(), prepared.prepared_at_ms)
            job = InspectionJob(
                event_id=prepared.event_id,
                trigger_id=prepared.trigger_id,
                station_id=prepared.station_id,
                camera_id=prepared.camera_id,
                triggered_at_ms=prepared.triggered_at_ms,
                received_at_ms=prepared.received_at_ms,
                source_timestamp_ms=prepared.source_timestamp_ms,
                prepared_at_ms=prepared.prepared_at_ms,
                created_at_ms=created_at_ms,
                artifacts=artifacts,
                selection={
                    "frame_id": prepared.frame_id,
                    "candidate_score": prepared.candidate_score.to_dict(),
                    "timing": dict(prepared.timing),
                },
                locator={
                    "label": prepared.label.to_dict(),
                    "crop_bbox": list(prepared.crop_bbox),
                    "orientation_degrees": prepared.orientation_degrees,
                },
                quality=prepared.quality.to_dict(),
                provenance={} if provenance is None else provenance,
            )
            job_bytes = _canonical_json_bytes(job.to_dict())
            job_manifest = FileManifest.from_bytes(
                job_bytes, content_type="application/json"
            )
            state = SpoolState(
                event_id=event_id,
                record_type=RecordType.INFERENCE_JOB,
                delivery_status=DeliveryStatus.LOCAL_ONLY,
                created_at_ms=created_at_ms,
                updated_at_ms=created_at_ms,
                files={
                    "selected_frame.jpg": selected_manifest,
                    "label_crop.png": crop_manifest,
                    "job.json": job_manifest,
                },
            )
            state_bytes = _canonical_json_bytes(state.to_dict())
            if self.limits is not None:
                self.check_capacity(
                    estimated_bytes=sum(
                        map(len, (selected_bytes, crop_bytes, job_bytes, state_bytes))
                    )
                )
            temp_path.mkdir(parents=False, exist_ok=False)
            self._write_bytes(temp_path / "selected_frame.jpg", selected_bytes)
            self._write_bytes(temp_path / "label_crop.png", crop_bytes)
            self._write_bytes(temp_path / "job.json", job_bytes)
            self._write_bytes(temp_path / "state.json", state_bytes)
            self._fsync_directory(temp_path)
            try:
                os.replace(temp_path, final_path)
            except OSError as exc:
                raise SpoolCommitError("Local spool atomic commit failed.") from exc
            self._fsync_directory(self.root)
            return SpoolRecord(path=final_path, state=state, job=job)
        except SpoolError:
            raise
        except Exception as exc:
            raise SpoolCommitError("Local spool commit failed.") from exc

    def _commit_terminal_outcome(
        self,
        outcome: PreparationOutcome,
        *,
        provenance: Mapping[str, Any] | None,
    ) -> SpoolRecord:
        event_id = outcome.event_id
        final_path = self.event_path(event_id)
        temp_path = self._contained_child(f".tmp_{event_id}")
        if self._lexists(final_path) or self._lexists(temp_path):
            raise SpoolConflictError(
                "Local spool already contains this event or an incomplete commit."
            )

        try:
            result = outcome.to_terminal_result()
            result_payload = dict(result.result_payload)
            if provenance is not None:
                result_payload["provenance"] = dict(provenance)
            if result_payload != dict(result.result_payload):
                result = replace(result, result_payload=result_payload)

            manifests: dict[str, FileManifest] = {}
            file_contents: dict[str, bytes] = {}
            if outcome.prepared is not None:
                selected_bytes = self._encode_image(
                    outcome.prepared.selected_frame, extension=".jpg"
                )
                crop_bytes = self._encode_image(
                    outcome.prepared.label_crop, extension=".png"
                )
                file_contents["selected_frame.jpg"] = selected_bytes
                file_contents["label_crop.png"] = crop_bytes
                manifests["selected_frame.jpg"] = FileManifest.from_bytes(
                    selected_bytes, content_type="image/jpeg"
                )
                manifests["label_crop.png"] = FileManifest.from_bytes(
                    crop_bytes, content_type="image/png"
                )

            result_bytes = _canonical_json_bytes(result.to_dict())
            file_contents["result.json"] = result_bytes
            manifests["result.json"] = FileManifest.from_bytes(
                result_bytes, content_type="application/json"
            )
            committed_at_ms = max(epoch_ms_now(), result.completed_at_ms)
            state = SpoolState(
                event_id=event_id,
                record_type=RecordType.TERMINAL_RESULT,
                delivery_status=DeliveryStatus.LOCAL_ONLY,
                created_at_ms=committed_at_ms,
                updated_at_ms=committed_at_ms,
                files=manifests,
            )
            state_bytes = _canonical_json_bytes(state.to_dict())
            if self.limits is not None:
                self.check_capacity(
                    estimated_bytes=sum(map(len, file_contents.values()))
                    + len(state_bytes)
                )
            temp_path.mkdir(parents=False, exist_ok=False)
            for name, content in file_contents.items():
                self._write_bytes(temp_path / name, content)
            self._write_bytes(temp_path / "state.json", state_bytes)
            self._fsync_directory(temp_path)
            try:
                os.replace(temp_path, final_path)
            except OSError as exc:
                raise SpoolCommitError("Local spool atomic commit failed.") from exc
            self._fsync_directory(self.root)
            return SpoolRecord(path=final_path, state=state, result=result)
        except SpoolError:
            raise
        except Exception as exc:
            raise SpoolCommitError("Local spool commit failed.") from exc

    def open_record(self, event_id: str) -> SpoolRecord:
        """Load and fully validate one committed event directory."""

        event_path = self.event_path(event_id)
        if not event_path.is_dir():
            raise SpoolCorruptionError(
                "Committed spool event directory is missing.",
                corruption_code="EVENT_DIRECTORY_MISSING",
            )
        if (event_path / ".state.json.tmp").exists():
            raise SpoolCorruptionError(
                "Spool event contains an incomplete state update.",
                corruption_code="INCOMPLETE_STATE_UPDATE",
            )

        state_payload = self._read_json_object(
            self._record_file(event_path, "state.json")
        )
        try:
            state = SpoolState.from_dict(state_payload)
        except Exception as exc:
            raise SpoolCorruptionError(
                "Spool state schema is invalid.",
                corruption_code="INVALID_STATE",
            ) from exc
        if state.event_id != event_id:
            raise SpoolCorruptionError(
                "Spool state event identity does not match its directory.",
                corruption_code="EVENT_ID_MISMATCH",
            )

        for name, expected in state.files.items():
            artifact_path = self._record_file(event_path, name)
            try:
                content = artifact_path.read_bytes()
            except OSError as exc:
                raise SpoolCorruptionError(
                    "A spool artifact could not be read.",
                    corruption_code="ARTIFACT_READ_ERROR",
                ) from exc
            actual = FileManifest.from_bytes(
                content, content_type=expected.content_type
            )
            if (
                actual.sha256 != expected.sha256
                or actual.size_bytes != expected.size_bytes
            ):
                raise SpoolCorruptionError(
                    "A spool artifact checksum or size does not match state.",
                    corruption_code="CHECKSUM_MISMATCH",
                )

        if state.record_type is RecordType.INFERENCE_JOB:
            required = {"selected_frame.jpg", "label_crop.png", "job.json"}
            if not required.issubset(state.files):
                raise SpoolCorruptionError(
                    "Inference spool record is missing required artifacts.",
                    corruption_code="REQUIRED_ARTIFACT_MISSING",
                )
            job_payload = self._read_json_object(
                self._record_file(event_path, "job.json")
            )
            try:
                job = InspectionJob.from_dict(job_payload)
            except Exception as exc:
                raise SpoolCorruptionError(
                    "Frozen inference job contract is invalid.",
                    corruption_code="INVALID_JOB",
                ) from exc
            if job.event_id != event_id:
                raise SpoolCorruptionError(
                    "Frozen job event identity does not match its directory.",
                    corruption_code="EVENT_ID_MISMATCH",
                )
            self._validate_job_artifacts(job, state)
            return SpoolRecord(path=event_path, state=state, job=job)

        if "result.json" not in state.files:
            raise SpoolCorruptionError(
                "Terminal spool record is missing result.json.",
                corruption_code="REQUIRED_ARTIFACT_MISSING",
            )
        result_payload = self._read_json_object(
            self._record_file(event_path, "result.json")
        )
        try:
            result = InspectionResult.from_dict(result_payload)
        except Exception as exc:
            raise SpoolCorruptionError(
                "Terminal inspection result contract is invalid.",
                corruption_code="INVALID_RESULT",
            ) from exc
        if result.event_id != event_id:
            raise SpoolCorruptionError(
                "Terminal result event identity does not match its directory.",
                corruption_code="EVENT_ID_MISMATCH",
            )
        return SpoolRecord(path=event_path, state=state, result=result)

    def advance_delivery(
        self, event_id: str, target: DeliveryStatus
    ) -> SpoolRecord:
        """Atomically advance delivery by one state; repeated targets are idempotent."""

        try:
            requested = DeliveryStatus(target)
        except (TypeError, ValueError) as exc:
            raise SpoolStateError("Requested delivery status is invalid.") from exc
        record = self.open_record(event_id)
        current = record.state.delivery_status
        if requested is current:
            return record
        if current is DeliveryStatus.LOCAL_ONLY:
            next_status = DeliveryStatus.ARTIFACTS_READY
        elif current is DeliveryStatus.ARTIFACTS_READY:
            next_status = (
                DeliveryStatus.JOB_PUBLISHED
                if record.record_type is RecordType.INFERENCE_JOB
                else DeliveryStatus.TERMINAL_RESULT_DURABLE
            )
        else:
            next_status = None
        if requested is not next_status:
            raise SpoolStateError(
                "Delivery status must advance exactly one monotonic step."
            )

        state = replace(
            record.state,
            delivery_status=requested,
            updated_at_ms=max(epoch_ms_now(), record.state.updated_at_ms + 1),
        )
        temporary = record.path / ".state.json.tmp"
        if self._lexists(temporary):
            raise SpoolStateError(
                "Spool event already contains an incomplete state update."
            )
        try:
            self._write_bytes(temporary, _canonical_json_bytes(state.to_dict()))
            os.replace(temporary, record.path / "state.json")
            self._fsync_directory(record.path)
        except OSError as exc:
            raise SpoolStateError("Local spool atomic state update failed.") from exc
        return replace(record, state=state)

    def scan_recovery(self) -> RecoveryReport:
        """Validate committed records and report incomplete evidence non-destructively."""

        pending: list[SpoolRecord] = []
        delivered: list[SpoolRecord] = []
        corrupt: list[RecoveryIssue] = []
        incomplete: list[Path] = []
        try:
            entries = sorted(self.root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise SpoolStateError("Local spool root cannot be scanned.") from exc

        for entry in entries:
            if entry.name.startswith(".tmp_"):
                incomplete.append(entry.resolve(strict=False))
                continue
            try:
                event_id = require_uuid(entry.name, "event_id")
            except ContractValidationError:
                corrupt.append(
                    RecoveryIssue(
                        event_id=entry.name,
                        code="INVALID_EVENT_DIRECTORY",
                        message="Spool root contains a non-event entry.",
                        path=entry.resolve(strict=False),
                    )
                )
                continue
            try:
                record = self.open_record(event_id)
            except SpoolCorruptionError as exc:
                corrupt.append(
                    RecoveryIssue(
                        event_id=event_id,
                        code=exc.corruption_code,
                        message=str(exc),
                        path=entry.resolve(strict=False),
                    )
                )
                continue
            except SpoolError as exc:
                corrupt.append(
                    RecoveryIssue(
                        event_id=event_id,
                        code=exc.code,
                        message=str(exc),
                        path=entry.resolve(strict=False),
                    )
                )
                continue
            if record.state.delivery_status in {
                DeliveryStatus.JOB_PUBLISHED,
                DeliveryStatus.TERMINAL_RESULT_DURABLE,
            }:
                delivered.append(record)
            else:
                pending.append(record)

        return RecoveryReport(
            pending_records=tuple(pending),
            delivered_records=tuple(delivered),
            corrupt_records=tuple(corrupt),
            incomplete_paths=tuple(incomplete),
        )

    def _record_file(self, event_path: Path, name: str) -> Path:
        candidate = event_path / name
        if candidate.is_symlink():
            raise SpoolCorruptionError(
                "Spool artifact symlinks are not allowed.",
                corruption_code="PATH_ESCAPE",
            )
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise SpoolCorruptionError(
                "Spool artifact path cannot be resolved.",
                corruption_code="PATH_ESCAPE",
            ) from exc
        if event_path not in resolved.parents:
            raise SpoolCorruptionError(
                "Spool artifact path resolves outside its event directory.",
                corruption_code="PATH_ESCAPE",
            )
        if not resolved.is_file():
            raise SpoolCorruptionError(
                "Required spool file is missing.",
                corruption_code="REQUIRED_ARTIFACT_MISSING",
            )
        return resolved

    @staticmethod
    def _read_json_object(path: Path) -> Mapping[str, Any]:
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpoolCorruptionError(
                "Spool JSON cannot be decoded.",
                corruption_code="INVALID_JSON",
            ) from exc
        if not isinstance(payload, Mapping):
            raise SpoolCorruptionError(
                "Spool JSON root must be an object.",
                corruption_code="INVALID_JSON",
            )
        return payload

    @staticmethod
    def _validate_job_artifacts(job: InspectionJob, state: SpoolState) -> None:
        mapping = {
            "selected_frame": "selected_frame.jpg",
            "label_crop": "label_crop.png",
        }
        for artifact_name, file_name in mapping.items():
            reference = job.artifacts.get(artifact_name)
            manifest = state.files.get(file_name)
            if reference is None or manifest is None:
                raise SpoolCorruptionError(
                    "Frozen job is missing a required artifact reference.",
                    corruption_code="REQUIRED_ARTIFACT_MISSING",
                )
            if (
                reference.sha256 != manifest.sha256
                or reference.size_bytes != manifest.size_bytes
                or reference.content_type != manifest.content_type
            ):
                raise SpoolCorruptionError(
                    "Frozen job artifact reference does not match spool state.",
                    corruption_code="ARTIFACT_REFERENCE_MISMATCH",
                )

    def _measure_usage(self) -> SpoolUsage:
        pending_events = 0
        pending_bytes = 0
        try:
            entries = tuple(self.root.iterdir())
            for entry in entries:
                if self._entry_is_delivered(entry):
                    continue
                pending_events += 1
                pending_bytes += self._entry_size(entry)
        except SpoolCapacityError:
            raise
        except OSError as exc:
            usage = SpoolUsage(
                pending_events=pending_events,
                pending_bytes=pending_bytes,
                free_disk_bytes=None,
            )
            raise SpoolCapacityError(
                "SPOOL_USAGE_PROBE_ERROR",
                "Local spool usage cannot be measured safely.",
                usage=usage,
            ) from exc

        usage_without_disk = SpoolUsage(
            pending_events=pending_events,
            pending_bytes=pending_bytes,
            free_disk_bytes=None,
        )
        try:
            free_disk_bytes = shutil.disk_usage(self.root).free
        except OSError as exc:
            raise SpoolCapacityError(
                "SPOOL_DISK_PROBE_ERROR",
                "Local spool free-disk capacity cannot be measured safely.",
                usage=usage_without_disk,
            ) from exc
        return replace(usage_without_disk, free_disk_bytes=free_disk_bytes)

    @staticmethod
    def _entry_is_delivered(entry: Path) -> bool:
        if entry.name.startswith(".tmp_") or entry.is_symlink() or not entry.is_dir():
            return False
        try:
            require_uuid(entry.name, "event_id")
            state_path = entry / "state.json"
            if state_path.is_symlink() or not state_path.is_file():
                return False
            payload = json.loads(state_path.read_bytes())
            state = SpoolState.from_dict(payload)
            return (
                state.event_id == entry.name
                and state.delivery_status
                in {
                    DeliveryStatus.JOB_PUBLISHED,
                    DeliveryStatus.TERMINAL_RESULT_DURABLE,
                }
            )
        except Exception:  # noqa: BLE001 - corrupt entries fail closed as pending
            return False

    @classmethod
    def _entry_size(cls, path: Path) -> int:
        if path.is_symlink():
            return path.lstat().st_size
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return path.lstat().st_size
        return sum(cls._entry_size(child) for child in path.iterdir())

    def _contained_child(self, name: str) -> Path:
        candidate = self.root / name
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise SpoolPathError("Unable to resolve a path inside spool root.") from exc
        if self.root not in resolved.parents:
            raise SpoolPathError("Resolved event path is outside spool root.")
        return resolved

    @staticmethod
    def _lexists(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @staticmethod
    def _encode_image(image: object, *, extension: str) -> bytes:
        array = np.asarray(image)
        if array.size == 0:
            raise SpoolCommitError("Local spool cannot encode an empty image.")
        options = (
            [cv2.IMWRITE_JPEG_QUALITY, 95]
            if extension == ".jpg"
            else [cv2.IMWRITE_PNG_COMPRESSION, 3]
        )
        success, encoded = cv2.imencode(extension, array, options)
        if not success:
            raise SpoolCommitError("Local spool image encoding failed.")
        return encoded.tobytes()

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError as exc:
            if _DIRECTORY_FSYNC_REQUIRED:
                raise SpoolCommitError(
                    "Local spool directory durability operation failed."
                ) from exc
            return
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if _DIRECTORY_FSYNC_REQUIRED:
                raise SpoolCommitError(
                    "Local spool directory durability operation failed."
                ) from exc
        finally:
            os.close(descriptor)

def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
