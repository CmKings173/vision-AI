"""Station orchestration through the local durability boundary."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

from ..camera.frame_buffer import FrameBuffer
from ..contracts import InspectionError, TriggerEvent
from .preparation import PreparationOutcome, StationPreparer
from .spool import LocalSpool, SpoolError, SpoolRecord


@dataclass(frozen=True)
class StationTriggerReport:
    event_id: str
    trigger_id: str
    durable_local: bool
    record: SpoolRecord
    spool_write_ms: float
    trigger_to_local_commit_ms: float


class StationTriggerFailure(RuntimeError):
    """A trigger was accepted and identified but local durability failed."""

    def __init__(
        self,
        *,
        event_id: str,
        trigger_id: str,
        error: InspectionError,
        processing_error: InspectionError | None = None,
    ) -> None:
        super().__init__(error.message)
        self.event_id = event_id
        self.trigger_id = trigger_id
        self.error = error
        self.processing_error = processing_error
        self.durable_local = False


class StationController:
    """Accept manual triggers and return only after an atomic local commit."""

    def __init__(
        self,
        *,
        frame_buffer: FrameBuffer,
        preparer: StationPreparer,
        spool: LocalSpool,
        station_id: str,
        camera_id: str,
        provenance: Mapping[str, object],
    ) -> None:
        self.frame_buffer = frame_buffer
        self.preparer = preparer
        self.spool = spool
        self.station_id = station_id
        self.camera_id = camera_id
        self.provenance = dict(provenance)

    def trigger(self) -> StationTriggerReport:
        # Capacity is checked before accepting the attempt. commit_outcome checks
        # again with the exact encoded byte projection to close the local race.
        self.spool.check_capacity(reserve_events=1)
        started = time.perf_counter()
        trigger = TriggerEvent.create(
            station_id=self.station_id,
            camera_id=self.camera_id,
        )
        try:
            packets = self.frame_buffer.snapshot(monotonic_now=time.monotonic())
            outcome = self.preparer.prepare_trigger(packets, trigger=trigger)
        except Exception:  # noqa: BLE001 - normalize normal post-trigger failures
            outcome = PreparationOutcome.internal_error(
                trigger=trigger,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        spool_started = time.perf_counter()
        try:
            record = self.spool.commit_outcome(outcome, provenance=self.provenance)
            spool_write_ms = (time.perf_counter() - spool_started) * 1000.0
        except SpoolError as exc:
            raise StationTriggerFailure(
                event_id=trigger.event_id,
                trigger_id=trigger.trigger_id,
                error=exc.to_inspection_error(),
                processing_error=outcome.error,
            ) from exc
        except Exception as exc:
            raise StationTriggerFailure(
                event_id=trigger.event_id,
                trigger_id=trigger.trigger_id,
                error=InspectionError(
                    code="SPOOL_COMMIT_ERROR",
                    stage="LOCAL_SPOOL",
                    message="Local spool commit failed.",
                    retryable=True,
                ),
                processing_error=outcome.error,
            ) from exc
        return StationTriggerReport(
            event_id=trigger.event_id,
            trigger_id=trigger.trigger_id,
            durable_local=True,
            record=record,
            spool_write_ms=spool_write_ms,
            trigger_to_local_commit_ms=(time.perf_counter() - started) * 1000.0,
        )
