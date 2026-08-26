"""Persistent station lifecycle and background delivery pump."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..camera.acquisition import CameraAcquisition
from ..camera.frame_buffer import FrameBuffer
from ..contracts import DeliveryStatus
from .controller import StationController, StationTriggerReport
from .dispatcher import OutboxDispatcher
from .spool import RecordType, SpoolError


@dataclass(frozen=True)
class PumpReport:
    uploaded_records: int
    published_jobs: int
    error_code: str | None = None
    delivery_health: str = "NOT_CHECKED"


class DeliveryPump:
    """Move committed records forward without blocking capture or triggers."""

    def __init__(
        self,
        *,
        dispatcher: OutboxDispatcher,
        publisher_factory: Callable[[], object],
        interval_s: float = 1.0,
        lifecycle_logger: Any | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self.dispatcher = dispatcher
        self.publisher_factory = publisher_factory
        self.interval_s = interval_s
        self.lifecycle_logger = lifecycle_logger
        self._publisher = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivery_health = "NOT_CHECKED"
        self.last_report = PumpReport(0, 0, delivery_health=self._delivery_health)

    @property
    def delivery_health(self) -> str:
        return self._delivery_health

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_once(self) -> PumpReport:
        uploaded = 0
        published = 0
        try:
            dispatch_reports = self.dispatcher.dispatch_pending()
            dispatch_error = next(
                (report.error_code for report in dispatch_reports if report.error_code),
                None,
            )
            if dispatch_error:
                self._delivery_health = "DEGRADED"
            elif dispatch_reports:
                self._delivery_health = "READY"
            for dispatch_report in dispatch_reports:
                self._log(
                    dispatch_report.event_id,
                    stage="ARTIFACT_DISPATCH",
                    status=(
                        "ERROR"
                        if dispatch_report.error_code
                        else dispatch_report.delivery_status.value
                    ),
                    delivery_status=dispatch_report.delivery_status.value,
                    uploaded_objects=dispatch_report.uploaded_objects,
                    artifact_upload_ms=dispatch_report.artifact_upload_ms,
                    error_code=dispatch_report.error_code,
                    delivery_health=self._delivery_health,
                )
            uploaded = sum(report.uploaded_objects > 0 for report in dispatch_reports)
            has_ready_job = any(
                record.record_type is RecordType.INFERENCE_JOB
                and record.state.delivery_status is DeliveryStatus.ARTIFACTS_READY
                for record in self.dispatcher.spool.scan_recovery().pending_records
            )
            if has_ready_job:
                if self._publisher is None:
                    self._publisher = self.publisher_factory()
                receipts = self._publisher.publish_pending()
                for receipt in receipts:
                    self._log(
                        receipt.event_id,
                        stage="JOB_PUBLISH",
                        status=DeliveryStatus.JOB_PUBLISHED.value,
                        publish_ms=receipt.publish_ms,
                        local_commit_to_published_ms=(
                            receipt.local_commit_to_published_ms
                        ),
                    )
                published = sum(not receipt.already_published for receipt in receipts)
            report = PumpReport(
                uploaded,
                published,
                error_code=dispatch_error,
                delivery_health=self._delivery_health,
            )
        except Exception as exc:  # noqa: BLE001 - keep background pump alive
            self._close_publisher()
            self._delivery_health = "DEGRADED"
            report = PumpReport(
                uploaded,
                published,
                error_code=str(getattr(exc, "code", type(exc).__name__)).upper(),
                delivery_health=self._delivery_health,
            )
        self.last_report = report
        return report

    def _log(self, event_id: str, *, stage: str, status: str, **fields) -> None:
        if self.lifecycle_logger is not None:
            self.lifecycle_logger.emit(
                event_id=event_id,
                component="station-service",
                stage=stage,
                status=status,
                **fields,
            )

    def start(self) -> None:
        if self.alive:
            raise RuntimeError("delivery pump is already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="vision-outbox-dispatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, join_timeout_s: float = 2.0) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0.0, join_timeout_s))
        stopped = not self.alive
        if stopped:
            self._close_publisher()
        return stopped

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_s)

    def _close_publisher(self) -> None:
        publisher, self._publisher = self._publisher, None
        close = getattr(publisher, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001,S110 - best-effort SDK shutdown
                pass


class StationService:
    """Own independent capture/delivery loops around the trigger controller."""

    def __init__(
        self,
        *,
        acquisition: CameraAcquisition,
        frame_buffer: FrameBuffer,
        controller: StationController,
        delivery_pump: DeliveryPump,
    ) -> None:
        self.acquisition = acquisition
        self.frame_buffer = frame_buffer
        self.controller = controller
        self.delivery_pump = delivery_pump

    def start(self) -> None:
        self.acquisition.start()
        self.delivery_pump.start()

    def wait_ready(self, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if (
                self.acquisition.alive
                and self.frame_buffer.latest(monotonic_now=time.monotonic()) is not None
            ):
                try:
                    self.controller.spool.check_capacity(reserve_events=1)
                except SpoolError:
                    return False
                return True
            time.sleep(0.02)
        return False

    def trigger(self) -> StationTriggerReport:
        return self.controller.trigger()

    def stop(self, *, timeout_s: float = 2.5) -> None:
        self.delivery_pump.stop(join_timeout_s=timeout_s)
        self.acquisition.stop(join_timeout_s=timeout_s)
