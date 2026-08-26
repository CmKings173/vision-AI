"""Resident inference worker over immutable distributed job contracts."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from ..contracts import (
    ArtifactRef,
    BusinessStatus,
    InspectionJob,
    ProcessingStatus,
    epoch_ms_now,
)
from ..contracts import InspectionResult as ContractInspectionResult
from ..contracts.core import thaw_json
from ..pipeline.types import PreparedInspection
from ..schemas import LabelCandidate, LabelCandidateScore, QualityReport
from ..storage import (
    ArtifactIntegrityError,
    ArtifactKeyPolicy,
    ArtifactStore,
    ObjectMetadata,
    StorageConflictError,
)
from .processor import InspectionProcessor
from .provenance import WorkerRuntimeDescriptor


class WorkerError(RuntimeError):
    retryable = False


class WorkerNotReadyError(WorkerError):
    retryable = True


class WorkerContractError(WorkerError):
    retryable = False


class WorkerResultConflictError(WorkerError):
    retryable = False


class ProfileMismatchError(WorkerError):
    code = "PROFILE_MISMATCH"
    retryable = False


class WorkerMessageTooLargeError(WorkerError):
    code = "MESSAGE_TOO_LARGE"
    retryable = False


class ImageTooLargeError(WorkerError):
    code = "IMAGE_TOO_LARGE"
    retryable = False


@dataclass(frozen=True)
class WorkerReport:
    event_id: str
    result: ContractInspectionResult
    result_reference: ArtifactRef
    durable_result: bool
    inference_skipped: bool
    artifact_download_ms: float
    checksum_ms: float
    image_decode_ms: float
    queue_wait_ms: float
    result_persist_ms: float
    worker_total_ms: float
    end_to_end_ms: float


class InferenceWorker:
    """Load inference dependencies once and process exact station crop bytes."""

    def __init__(
        self,
        *,
        processor: InspectionProcessor,
        store: ArtifactStore,
        artifact_policy: ArtifactKeyPolicy | None = None,
        runtime_descriptor: WorkerRuntimeDescriptor | None = None,
        max_job_message_bytes: int = 1024 * 1024,
        max_image_pixels: int = 16_000_000,
    ) -> None:
        self.processor = processor
        self.store = store
        self.artifact_policy = artifact_policy or ArtifactKeyPolicy(
            bucket="vision-inspections"
        )
        self.runtime_descriptor = (
            runtime_descriptor or WorkerRuntimeDescriptor.from_processor(processor)
        )
        for name, value in (
            ("max_job_message_bytes", max_job_message_bytes),
            ("max_image_pixels", max_image_pixels),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.max_job_message_bytes = max_job_message_bytes
        self.max_image_pixels = max_image_pixels
        self.ready = False
        self.startup_ms: float | None = None

    def start(self) -> None:
        if self.ready:
            return
        started = time.perf_counter()
        warmup = getattr(self.processor.ocr, "warmup", None)
        prepare_barcode = getattr(self.processor.barcode, "prepare", None)
        if not callable(warmup) or not callable(prepare_barcode):
            raise WorkerNotReadyError(
                "Worker dependencies do not expose required readiness hooks."
            )
        try:
            warmup_result = warmup()
            ocr_ready = bool(
                getattr(warmup_result, "success", False)
                and getattr(self.processor.ocr, "ready", False)
            )
            barcode_ready = bool(prepare_barcode())
        except Exception as exc:
            raise WorkerNotReadyError("Worker dependency warmup failed.") from exc
        if not ocr_ready or not barcode_ready:
            raise WorkerNotReadyError("Worker dependencies did not become ready.")
        self.startup_ms = (time.perf_counter() - started) * 1000.0
        self.ready = True

    def process_message(self, body: bytes) -> WorkerReport:
        if not self.ready:
            raise WorkerNotReadyError("Worker is not ready.")
        worker_started = time.perf_counter()
        job = _parse_job(body, max_bytes=self.max_job_message_bytes)
        try:
            self.runtime_descriptor.assert_compatible(job.provenance)
        except (TypeError, ValueError) as exc:
            raise ProfileMismatchError(
                "Requested profile is incompatible with the active worker runtime."
            ) from exc
        self.artifact_policy.validate_job(job)
        queue_wait_ms = float(max(0, epoch_ms_now() - job.created_at_ms))

        existing = self._load_existing_result(job)
        if existing is not None:
            result, reference = existing
            return WorkerReport(
                event_id=job.event_id,
                result=result,
                result_reference=reference,
                durable_result=True,
                inference_skipped=True,
                artifact_download_ms=0.0,
                checksum_ms=0.0,
                image_decode_ms=0.0,
                queue_wait_ms=queue_wait_ms,
                result_persist_ms=0.0,
                worker_total_ms=(time.perf_counter() - worker_started) * 1000.0,
                end_to_end_ms=float(max(0, epoch_ms_now() - job.triggered_at_ms)),
            )

        download_started = time.perf_counter()
        crop_reference = job.artifacts["label_crop"]
        crop_bytes = self.store.get_verified(
            crop_reference,
            max_bytes=self.artifact_policy.max_label_crop_bytes,
        )
        artifact_download_ms = (time.perf_counter() - download_started) * 1000.0
        checksum_started = time.perf_counter()
        # The store verifies the immutable SHA-256. This separately measured
        # pass keeps checksum timing explicit for worker observability.
        if hashlib.sha256(crop_bytes).hexdigest() != crop_reference.sha256:
            raise ArtifactIntegrityError(
                "Downloaded crop checksum does not match the job reference."
            )
        checksum_ms = (time.perf_counter() - checksum_started) * 1000.0
        decode_started = time.perf_counter()
        label_crop = _decode_lossless_crop(
            crop_bytes, max_image_pixels=self.max_image_pixels
        )
        image_decode_ms = (time.perf_counter() - decode_started) * 1000.0

        prepared = _prepared_from_job(job, label_crop)
        processing_started_at_ms = epoch_ms_now()
        local_result = self.processor.process(prepared)
        try:
            business_status = BusinessStatus(local_result.validation.status)
        except ValueError as exc:
            raise WorkerContractError(
                "Processor returned an unsupported business status."
            ) from exc
        completed_at_ms = max(epoch_ms_now(), processing_started_at_ms)
        local_timing = dict(local_result.timing)
        result = ContractInspectionResult(
            event_id=job.event_id,
            trigger_id=job.trigger_id,
            station_id=job.station_id,
            camera_id=job.camera_id,
            processing_status=ProcessingStatus.COMPLETED,
            business_status=business_status,
            inference_executed=True,
            created_at_ms=processing_started_at_ms,
            completed_at_ms=completed_at_ms,
            reasons=tuple(local_result.validation.reasons),
            quality=local_result.quality.to_dict(),
            result_payload={
                "inspection": local_result.to_dict(),
                "producer_provenance": dict(job.provenance),
                "worker_runtime_provenance": self.runtime_descriptor.to_dict(),
                "stage_timings": {
                    "queue_wait_ms": queue_wait_ms,
                    "artifact_download_ms": artifact_download_ms,
                    "checksum_ms": checksum_ms,
                    "image_decode_ms": image_decode_ms,
                    "ocr_ms": float(local_timing.get("ocr_ms", 0.0)),
                    "barcode_ms": float(local_timing.get("barcode_ms", 0.0)),
                    "extraction_ms": float(
                        local_timing.get("field_extraction_ms", 0.0)
                    ),
                    "validation_ms": float(
                        local_timing.get("validation_ms", 0.0)
                    ),
                    "processing_total_ms": float(
                        local_timing.get("total_ms", 0.0)
                    ),
                },
                "worker": {
                    "processor": type(self.processor).__name__,
                    "ocr_engine": getattr(self.processor.ocr, "engine", "unknown"),
                    "startup_ms_excluded": self.startup_ms,
                    "artifact_download_ms": artifact_download_ms,
                },
            },
        )
        result_bytes = _canonical_json_bytes(result.to_dict())
        result_reference = self.artifact_policy.result_reference(job, result_bytes)
        persist_started = time.perf_counter()
        try:
            self.store.put_if_absent(result_reference, result_bytes)
        except StorageConflictError:
            raced = self._load_existing_result(job)
            if raced is None:
                raise
            result, result_reference = raced
            inference_skipped = True
        else:
            self.store.get_verified(result_reference)
            inference_skipped = False
        result_persist_ms = (time.perf_counter() - persist_started) * 1000.0
        return WorkerReport(
            event_id=job.event_id,
            result=result,
            result_reference=result_reference,
            durable_result=True,
            inference_skipped=inference_skipped,
            artifact_download_ms=artifact_download_ms,
            checksum_ms=checksum_ms,
            image_decode_ms=image_decode_ms,
            queue_wait_ms=queue_wait_ms,
            result_persist_ms=result_persist_ms,
            worker_total_ms=(time.perf_counter() - worker_started) * 1000.0,
            end_to_end_ms=float(max(0, epoch_ms_now() - job.triggered_at_ms)),
        )

    def _load_existing_result(
        self, job: InspectionJob
    ) -> tuple[ContractInspectionResult, ArtifactRef] | None:
        result_bucket, result_key = self.artifact_policy.result_location(job)
        metadata = self.store.head(result_bucket, result_key)
        if metadata is None:
            return None
        reference = _reference_from_metadata(metadata)
        try:
            content = self.store.get_verified(
                reference,
                max_bytes=self.max_job_message_bytes,
            )
            payload = json.loads(content)
            result = ContractInspectionResult.from_dict(payload)
        except Exception as exc:
            raise WorkerResultConflictError(
                "Existing durable result is invalid or corrupt."
            ) from exc
        if (
            result.event_id != job.event_id
            or result.trigger_id != job.trigger_id
            or result.station_id != job.station_id
            or result.camera_id != job.camera_id
        ):
            raise WorkerResultConflictError(
                "Existing durable result identity conflicts with the job."
            )
        existing_runtime = thaw_json(
            result.result_payload.get("worker_runtime_provenance")
        )
        if existing_runtime != self.runtime_descriptor.to_dict():
            raise WorkerResultConflictError(
                "Existing durable result provenance conflicts with worker runtime."
            )
        return result, reference


def _parse_job(body: bytes, *, max_bytes: int) -> InspectionJob:
    if not isinstance(body, bytes):
        raise WorkerContractError("Inspection job body must be bytes.")
    if len(body) > max_bytes:
        raise WorkerMessageTooLargeError(
            "Inspection job exceeds the configured message size limit."
        )
    try:
        payload = json.loads(body)
        if not isinstance(payload, Mapping):
            raise TypeError("job root is not an object")
        return InspectionJob.from_dict(payload)
    except Exception as exc:
        raise WorkerContractError("Inspection job contract is invalid.") from exc


def _decode_lossless_crop(content: bytes, *, max_image_pixels: int):
    if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        raise WorkerContractError("Exact label crop is not a PNG image.")
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width < 1 or height < 1:
        raise WorkerContractError("Exact label crop has invalid PNG dimensions.")
    if width * height > max_image_pixels:
        raise ImageTooLargeError(
            "Exact label crop exceeds the configured pixel limit."
        )
    encoded = np.frombuffer(content, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
    if image is None or image.size == 0:
        raise WorkerContractError("Exact label crop is not a valid image.")
    return image


def _prepared_from_job(job: InspectionJob, label_crop: object) -> PreparedInspection:
    try:
        selection = job.selection
        locator = job.locator
        label_payload = _mapping(locator["label"], "locator.label")
        score_payload = _mapping(
            selection["candidate_score"], "selection.candidate_score"
        )
        quality_payload = _mapping(job.quality, "quality")
        bbox = _float_tuple(label_payload["bbox"], 4, "label.bbox")
        crop_bbox = _float_tuple(locator["crop_bbox"], 4, "crop_bbox")
        corners_payload = label_payload.get("corners")
        corners = (
            None
            if corners_payload is None
            else tuple(
                _float_tuple(point, 2, "label.corners") for point in corners_payload
            )
        )
        label = LabelCandidate(
            bbox=bbox,
            confidence=float(label_payload["confidence"]),
            detector=str(label_payload["detector"]),
            frame_id=label_payload.get("frame_id"),
            corners=corners,
        )
        score = LabelCandidateScore(
            **{
                name: float(score_payload[name])
                for name in (
                    "total",
                    "detection_confidence",
                    "crop_sharpness",
                    "crop_exposure",
                    "crop_glare",
                    "label_area_ratio",
                    "frame_freshness",
                    "crop_validity",
                )
            }
        )
        quality = QualityReport(
            status=str(quality_payload["status"]),
            state=quality_payload.get("state"),
            sharpness=quality_payload.get("sharpness"),
            brightness=quality_payload.get("brightness"),
            underexposed_ratio=quality_payload.get("underexposed_ratio"),
            overexposed_ratio=quality_payload.get("overexposed_ratio"),
            glare_ratio=quality_payload.get("glare_ratio"),
            width=quality_payload.get("width"),
            height=quality_payload.get("height"),
            area=quality_payload.get("area"),
            reasons=tuple(quality_payload.get("reasons", ())),
        )
        frame_id = int(selection["frame_id"])
        timing = {
            str(name): float(value)
            for name, value in _mapping(selection.get("timing", {}), "timing").items()
        }
        orientation = int(locator.get("orientation_degrees", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkerContractError("Prepared job metadata is invalid.") from exc
    return PreparedInspection(
        event_id=job.event_id,
        trigger_id=job.trigger_id,
        station_id=job.station_id,
        camera_id=job.camera_id,
        triggered_at_ms=job.triggered_at_ms,
        received_at_ms=job.received_at_ms,
        source_timestamp_ms=job.source_timestamp_ms,
        prepared_at_ms=job.prepared_at_ms,
        selected_frame=None,
        label_crop=label_crop,
        frame_id=frame_id,
        label=label,
        crop_bbox=crop_bbox,
        candidate_score=score,
        quality=quality,
        timing=timing,
        orientation_degrees=orientation,
    )


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkerContractError(f"{field_name} must be an object")
    return value


def _float_tuple(value: Any, length: int, field_name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise WorkerContractError(f"{field_name} has an invalid shape")
    return tuple(float(item) for item in value)


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reference_from_metadata(metadata: ObjectMetadata) -> ArtifactRef:
    return ArtifactRef(
        bucket=metadata.bucket,
        key=metadata.key,
        sha256=metadata.sha256,
        content_type=metadata.content_type,
        size_bytes=metadata.size_bytes,
    )
