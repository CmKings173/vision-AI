import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import DeliveryStatus, ProcessingStatus
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import FramePacket
from label_inspection.station.controller import StationController, StationTriggerFailure
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.spool import LocalSpool, SpoolCommitError
from tests.fixtures.quality import sharp_label


def _phase2_station_config(tmp_path):
    from label_inspection.config import Settings

    return replace(
        Settings(),
        spool_root=str(tmp_path / "spool"),
        spool_max_pending_events=10,
        spool_max_pending_bytes=10_000_000,
        spool_min_free_disk_bytes=0,
        buffer_window_ms=1_000,
        max_frame_age_ms=1_000,
        detector="fixed-roi",
        label_roi="0.05,0.05,0.95,0.95",
        minio_endpoint="127.0.0.1:9000",
        minio_access_key="test-access",
        minio_secret_key="test-secret",
        rabbitmq_url="amqp://vision:test@127.0.0.1:5672/%2F",
        dispatch_interval_s=60.0,
    )


class _CompositionCamera:
    def __init__(self, source, **kwargs):
        self.source = source
        self._closed = False

    def read(self):
        if self._closed:
            return None
        return FramePacket(
            frame_id=1,
            captured_at=time.time(),
            frame=np.asarray(sharp_label()),
            source=self.source,
            captured_monotonic=time.monotonic(),
        )

    def close(self):
        self._closed = True


def _controller(tmp_path):
    buffer = FrameBuffer(max_size=4, window_ms=1000)
    buffer.append(
        FramePacket(
            frame_id=1,
            captured_at=time.time() - 0.01,
            frame=sharp_label(),
            source="rtsp",
            captured_monotonic=time.monotonic(),
        )
    )
    preparer = StationPreparer(
        detector=FixedROIDetector((0.05, 0.05, 0.95, 0.95)),
        selector=FrameSelector(top_k=1, score_fn=lambda image: 1.0),
        quality_checker=QualityChecker(
            min_width=1,
            min_height=1,
            min_brightness=0,
            max_brightness=255,
            min_sharpness=0,
            max_underexposed_ratio=1,
            max_overexposed_ratio=1,
            max_glare_ratio=1,
        ),
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        station_id="STATION-01",
        camera_id="PHONE-01",
        bbox_padding_ratio=0,
    )
    spool = LocalSpool(tmp_path / "spool")
    return StationController(
        frame_buffer=buffer,
        preparer=preparer,
        spool=spool,
        station_id="STATION-01",
        camera_id="PHONE-01",
        provenance={"profile": "dgx_spark_label", "extractor_version": "v1"},
    ), spool


def test_station_trigger_returns_after_local_commit_without_network(tmp_path):
    controller, spool = _controller(tmp_path)

    report = controller.trigger()

    assert report.durable_local is True
    assert report.record.state.delivery_status is DeliveryStatus.LOCAL_ONLY
    assert report.spool_write_ms >= 0
    assert spool.open_record(report.event_id).frozen_job_bytes() == report.record.frozen_job_bytes()


def test_each_accepted_trigger_gets_distinct_event_and_trigger_identity(tmp_path):
    controller, _ = _controller(tmp_path)

    first = controller.trigger()
    second = controller.trigger()

    assert first.event_id != second.event_id
    assert first.trigger_id != second.trigger_id


def test_station_controller_source_has_no_inference_runtime_imports():
    from pathlib import Path

    source = Path("src/label_inspection/station/controller.py").read_text("utf-8")

    assert "label_inspection.ocr" not in source
    assert "label_inspection.barcode" not in source
    assert "build_processor" not in source


def test_station_entrypoint_does_not_require_minio_probe_before_camera_start():
    from pathlib import Path

    source = Path("scripts/run_station.py").read_text("utf-8")
    setup_before_frame_buffer = source.split("frame_buffer = FrameBuffer", 1)[0]

    assert "store.ensure_bucket" not in setup_before_frame_buffer


@pytest.mark.parametrize("minio_failure", ["connect", "validate"])
def test_station_composition_starts_capture_and_commits_when_minio_is_unavailable(
    tmp_path, monkeypatch, minio_failure
):
    from scripts import run_station

    config = _phase2_station_config(tmp_path)
    config.validate_phase2_station()
    monkeypatch.setattr(run_station, "RTSPCamera", _CompositionCamera)
    connect_calls = []

    def fail_minio_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        raise RuntimeError("MinIO unavailable")

    class _FailingValidationStore:
        def validate_bucket(self, bucket):
            raise RuntimeError("MinIO bucket unavailable")

    if minio_failure == "connect":
        monkeypatch.setattr(
            run_station.MinioArtifactStore,
            "connect",
            staticmethod(fail_minio_connect),
        )
    else:
        def validate_failing_connect(*args, **kwargs):
            connect_calls.append((args, kwargs))
            return _FailingValidationStore()

        monkeypatch.setattr(
            run_station.MinioArtifactStore,
            "connect",
            staticmethod(validate_failing_connect),
        )

    runtime = run_station.build_station_runtime(config, "rtsp://camera")
    assert connect_calls == []

    runtime.service.start()
    try:
        assert runtime.service.wait_ready(timeout_s=2.0)
        assert runtime.service.acquisition.alive

        report = runtime.service.trigger()
        assert report.durable_local is True
        assert report.record.state.delivery_status is DeliveryStatus.LOCAL_ONLY

        delivery = runtime.delivery_pump.run_once()
        assert delivery.delivery_health == "DEGRADED"
        assert connect_calls
        assert runtime.spool.open_record(report.event_id).state.delivery_status is DeliveryStatus.LOCAL_ONLY
    finally:
        runtime.service.stop(timeout_s=1.0)


@pytest.mark.parametrize("failure_stage", ["channel", "topology", "publisher"])
def test_publisher_factory_closes_connection_after_construction_failure(
    tmp_path, monkeypatch, failure_stage
):
    from scripts import run_station

    config = _phase2_station_config(tmp_path)

    class _Connection:
        def __init__(self):
            self.is_open = True
            self.close_calls = 0

        def channel(self):
            if failure_stage == "channel":
                raise RuntimeError("channel setup failed")
            return object()

        def close(self):
            self.close_calls += 1
            self.is_open = False

    connection = _Connection()
    monkeypatch.setitem(
        sys.modules,
        "pika",
        SimpleNamespace(
            URLParameters=lambda value: value,
            BlockingConnection=lambda parameters: connection,
        ),
    )

    if failure_stage == "topology":
        class _FailingTopology:
            def __init__(self, config):
                raise RuntimeError("topology setup failed")

            def declare(self, channel):
                raise AssertionError("topology declaration should not be reached")

        monkeypatch.setattr(run_station, "RabbitTopology", _FailingTopology)
    elif failure_stage == "publisher":
        class _WorkingTopology:
            def __init__(self, config):
                self.config = config

            def declare(self, channel):
                return None

        def fail_publisher(channel):
            raise RuntimeError("publisher setup failed")

        monkeypatch.setattr(run_station, "RabbitTopology", _WorkingTopology)
        monkeypatch.setattr(run_station, "PikaConfirmedPublisher", fail_publisher)

    with pytest.raises(RuntimeError):
        run_station._publisher_factory(config, object())()

    assert connection.close_calls == 1


def test_post_acceptance_spool_failure_preserves_event_identity_and_safe_error(
    tmp_path, monkeypatch
):
    controller, _ = _controller(tmp_path)

    def fail_commit(*args, **kwargs):
        raise SpoolCommitError("Local spool commit failed.")

    monkeypatch.setattr(controller.spool, "commit_outcome", fail_commit)

    try:
        controller.trigger()
    except StationTriggerFailure as failure:
        assert failure.event_id
        assert failure.trigger_id
        assert failure.error.code == "SPOOL_COMMIT_ERROR"
        assert failure.error.retryable is True
    else:
        raise AssertionError("accepted trigger failure must preserve identity")


@pytest.mark.parametrize(
    "fault",
    ["file_fsync", "rename", "parent_open", "parent_fsync"],
)
def test_durability_fault_never_returns_durable_local_true(
    tmp_path, monkeypatch, fault
):
    controller, spool = _controller(tmp_path)

    if fault == "file_fsync":
        monkeypatch.setattr(
            spool,
            "_write_bytes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                SpoolCommitError("Local spool file durability failed.")
            ),
        )
    elif fault == "rename":
        monkeypatch.setattr(
            "label_inspection.station.spool.os.replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("simulated rename failure")
            ),
        )
    else:
        message = (
            "Local spool directory open durability failed."
            if fault == "parent_open"
            else "Local spool directory fsync durability failed."
        )
        original_sync = spool._fsync_directory

        def fail_parent_sync(path):
            if path == spool.root:
                raise SpoolCommitError(message)
            return original_sync(path)

        monkeypatch.setattr(spool, "_fsync_directory", fail_parent_sync)

    with pytest.raises(StationTriggerFailure) as raised:
        controller.trigger()

    failure = raised.value
    assert failure.event_id
    assert failure.trigger_id
    assert failure.error.code == "SPOOL_COMMIT_ERROR"


def test_selector_runtime_error_becomes_durable_terminal_result_with_identities(
    tmp_path, monkeypatch
):
    controller, spool = _controller(tmp_path)

    def fail_selector(*_args, **_kwargs):
        raise RuntimeError("secret selector internals")

    monkeypatch.setattr(controller.preparer.selector, "select", fail_selector)

    report = controller.trigger()

    assert report.event_id
    assert report.trigger_id
    assert report.durable_local is True
    assert report.record.result is not None
    assert report.record.result.event_id == report.event_id
    assert report.record.result.trigger_id == report.trigger_id
    assert report.record.result.processing_status is ProcessingStatus.ERROR
    assert report.record.result.business_status is None
    assert report.record.result.error is not None
    assert report.record.result.error.code == "INTERNAL_ERROR"
    assert "secret" not in report.record.result.error.message.lower()
    assert spool.open_record(report.event_id).result == report.record.result


def test_processing_and_terminal_spool_failure_preserve_both_errors_and_ids(
    tmp_path, monkeypatch
):
    controller, _ = _controller(tmp_path)

    monkeypatch.setattr(
        controller.preparer.selector,
        "select",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret processing internals")
        ),
    )
    monkeypatch.setattr(
        controller.spool,
        "commit_outcome",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SpoolCommitError("Local spool commit failed.")
        ),
    )

    with pytest.raises(StationTriggerFailure) as raised:
        controller.trigger()

    failure = raised.value
    assert failure.event_id
    assert failure.trigger_id
    assert failure.durable_local is False
    assert failure.error.code == "SPOOL_COMMIT_ERROR"
    assert failure.processing_error is not None
    assert failure.processing_error.code == "INTERNAL_ERROR"
    assert "secret" not in failure.processing_error.message.lower()


def test_controller_does_not_catch_base_exception_from_preparation(
    tmp_path, monkeypatch
):
    controller, _ = _controller(tmp_path)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(controller.preparer.selector, "select", interrupt)

    with pytest.raises(KeyboardInterrupt):
        controller.trigger()
