"""Recoverable local-spool to object-storage dispatcher."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..contracts import ArtifactRef, DeliveryStatus
from ..storage import ArtifactStore, event_object_keys
from .spool import LocalSpool, RecordType, SpoolRecord


@dataclass(frozen=True)
class DispatchReport:
    event_id: str
    uploaded_objects: int
    message_required: bool
    already_delivered: bool
    delivery_status: DeliveryStatus
    artifact_upload_ms: float = 0.0
    error_code: str | None = None


class OutboxDispatcher:
    """Upload immutable spool records without reconstructing their contracts."""

    def __init__(self, *, spool: LocalSpool, store: ArtifactStore) -> None:
        self.spool = spool
        self.store = store

    def dispatch_record(self, record: SpoolRecord) -> DispatchReport:
        current = self.spool.open_record(record.state.event_id)
        message_required = current.record_type is RecordType.INFERENCE_JOB
        terminal_status = (
            DeliveryStatus.JOB_PUBLISHED
            if message_required
            else DeliveryStatus.TERMINAL_RESULT_DURABLE
        )
        if current.state.delivery_status is terminal_status:
            return DispatchReport(
                event_id=current.state.event_id,
                uploaded_objects=0,
                message_required=message_required,
                already_delivered=True,
                delivery_status=current.state.delivery_status,
            )

        uploads: tuple[tuple[ArtifactRef, str], ...] = ()
        upload_started = time.perf_counter()
        artifacts_already_ready = (
            current.state.delivery_status is DeliveryStatus.ARTIFACTS_READY
        )
        if current.state.delivery_status is DeliveryStatus.LOCAL_ONLY:
            uploads = self._uploads(current)
            for reference, file_name in uploads:
                self.store.put_if_absent(
                    reference, (current.path / file_name).read_bytes()
                )

            current = self.spool.advance_delivery(
                current.state.event_id, DeliveryStatus.ARTIFACTS_READY
            )
        elif current.state.delivery_status is not DeliveryStatus.ARTIFACTS_READY:
            raise RuntimeError("Spool record has an invalid delivery state.")
        if current.record_type is RecordType.TERMINAL_RESULT:
            current = self.spool.advance_delivery(
                current.state.event_id,
                DeliveryStatus.TERMINAL_RESULT_DURABLE,
            )
        return DispatchReport(
            event_id=current.state.event_id,
            uploaded_objects=len(uploads),
            message_required=message_required,
            already_delivered=artifacts_already_ready,
            delivery_status=current.state.delivery_status,
            artifact_upload_ms=(time.perf_counter() - upload_started) * 1000.0,
        )

    def dispatch_pending(self) -> tuple[DispatchReport, ...]:
        """Dispatch every valid record so one poisoned event cannot starve others."""

        recovery = self.spool.scan_recovery()
        reports: list[DispatchReport] = []
        for record in recovery.pending_records:
            try:
                reports.append(self.dispatch_record(record))
            except Exception as exc:  # noqa: BLE001 - isolate poisoned records
                reports.append(
                    DispatchReport(
                        event_id=record.state.event_id,
                        uploaded_objects=0,
                        message_required=(
                            record.record_type is RecordType.INFERENCE_JOB
                        ),
                        already_delivered=False,
                        delivery_status=record.state.delivery_status,
                        error_code=str(
                            getattr(exc, "code", type(exc).__name__)
                        ).upper(),
                    )
                )
        return tuple(reports)

    def _uploads(self, record: SpoolRecord) -> tuple[tuple[ArtifactRef, str], ...]:
        occurred_at_ms = (
            record.job.triggered_at_ms
            if record.job is not None
            else record.result.created_at_ms
            if record.result is not None
            else record.state.created_at_ms
        )
        station_id = (
            record.job.station_id
            if record.job is not None
            else record.result.station_id
            if record.result is not None
            else "unknown"
        )
        keys = event_object_keys(
            station_id=station_id,
            event_id=record.state.event_id,
            occurred_at_ms=occurred_at_ms,
        )

        if record.record_type is RecordType.INFERENCE_JOB:
            assert record.job is not None
            manifest = record.state.files["job.json"]
            return (
                (record.job.artifacts["selected_frame"], "selected_frame.jpg"),
                (record.job.artifacts["label_crop"], "label_crop.png"),
                (
                    ArtifactRef(
                        bucket=self.spool.bucket,
                        key=keys.job,
                        sha256=manifest.sha256,
                        content_type=manifest.content_type,
                        size_bytes=manifest.size_bytes,
                    ),
                    "job.json",
                ),
            )

        manifest_to_key = {
            "selected_frame.jpg": keys.selected_frame,
            "label_crop.png": keys.label_crop,
            "result.json": keys.result,
        }
        uploads: list[tuple[ArtifactRef, str]] = []
        for file_name in ("selected_frame.jpg", "label_crop.png", "result.json"):
            manifest = record.state.files.get(file_name)
            if manifest is None:
                continue
            uploads.append(
                (
                    ArtifactRef(
                        bucket=self.spool.bucket,
                        key=manifest_to_key[file_name],
                        sha256=manifest.sha256,
                        content_type=manifest.content_type,
                        size_bytes=manifest.size_bytes,
                    ),
                    file_name,
                )
            )
        return tuple(uploads)
