from pathlib import Path
from types import SimpleNamespace

import scripts.manual_rtsp_inspection as manual_rtsp
from scripts.manual_rtsp_inspection import (
    _new_runtime_metrics,
    _record_inspection_metrics,
    _record_stale_metrics,
    _runtime_metrics_summary,
)


def test_runtime_metrics_exclude_detector_misses_from_ocr_and_e2e_latency():
    metrics = _new_runtime_metrics()
    _record_stale_metrics(metrics)
    _record_inspection_metrics(
        metrics,
        detector_attempts=[
            {"inference_ms": 12.0, "accepted_detection_count": 0},
            {"inference_ms": 14.0, "accepted_detection_count": 1},
        ],
        result=SimpleNamespace(
            raw_ocr=SimpleNamespace(state="NOT_RUN", success=False),
            barcode=SimpleNamespace(state="NOT_RUN", success=False),
            timing={
                "ocr_ms": 0.0,
                "barcode_ms": 0.0,
                "parallel_inference_ms": 0.0,
            },
            validation=SimpleNamespace(status="REVIEW"),
        ),
        inspection_ms=20.0,
    )

    summary = _runtime_metrics_summary(metrics)

    assert summary["total_triggers"] == 2
    assert summary["stale_trigger_count"] == 1
    assert summary["detection_attempts"] == 2
    assert summary["detection_hits"] == 1
    assert summary["detection_misses"] == 1
    assert summary["ocr_attempts"] == 0
    assert summary["ocr_successes"] == 0
    assert summary["ocr_failures"] == 0
    assert summary["barcode_attempts"] == 0
    assert summary["full_pipeline_attempts"] == 0
    assert summary["full_pipeline_passes"] == 0
    assert summary["detection_p50_ms"] == 12.0
    assert summary["ocr_p50_ms"] is None
    assert summary["barcode_p50_ms"] is None
    assert summary["parallel_inference_p50_ms"] is None
    assert summary["total_inspection_p50_ms"] is None
    assert summary["successful_e2e_p50_ms"] is None


def test_runtime_metrics_count_executed_ocr_failure_and_successful_e2e_separately():
    metrics = _new_runtime_metrics()
    _record_inspection_metrics(
        metrics,
        detector_attempts=[{"inference_ms": 10.0, "accepted_detection_count": 1}],
        result=SimpleNamespace(
            raw_ocr=SimpleNamespace(state="FAILED", success=False),
            barcode=SimpleNamespace(state="FAILED", success=False),
            timing={
                "ocr_ms": 31.0,
                "barcode_ms": 9.0,
                "parallel_inference_ms": 33.0,
            },
            validation=SimpleNamespace(status="REVIEW"),
        ),
        inspection_ms=45.0,
    )
    _record_inspection_metrics(
        metrics,
        detector_attempts=[{"inference_ms": 11.0, "accepted_detection_count": 1}],
        result=SimpleNamespace(
            raw_ocr=SimpleNamespace(state="SUCCESS", success=True),
            barcode=SimpleNamespace(state="SUCCESS", success=True),
            timing={
                "ocr_ms": 32.0,
                "barcode_ms": 10.0,
                "parallel_inference_ms": 34.0,
            },
            validation=SimpleNamespace(status="PASS"),
        ),
        inspection_ms=46.0,
    )

    summary = _runtime_metrics_summary(metrics)

    assert summary["ocr_attempts"] == 2
    assert summary["ocr_successes"] == 1
    assert summary["ocr_failures"] == 1
    assert summary["barcode_attempts"] == 2
    assert summary["barcode_successes"] == 1
    assert summary["barcode_failures"] == 1
    assert summary["full_pipeline_attempts"] == 2
    assert summary["full_pipeline_passes"] == 1
    assert summary["ocr_p50_ms"] == 31.0
    assert summary["barcode_p50_ms"] == 9.0
    assert summary["parallel_inference_p50_ms"] == 33.0
    assert summary["total_inspection_p50_ms"] == 45.0
    assert summary["successful_e2e_p50_ms"] == 46.0


def test_wait_for_ready_recovers_after_an_initial_stale_snapshot(monkeypatch):
    snapshots = iter([[], ["fresh-frame"]])
    health = iter(
        [
            SimpleNamespace(connected=True, stale=True),
            SimpleNamespace(connected=True, stale=False),
        ]
    )

    class FakeBuffer:
        def snapshot(self, **kwargs):
            return next(snapshots)

    class FakeCamera:
        @property
        def health(self):
            return next(health)

    monkeypatch.setattr(manual_rtsp.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(manual_rtsp.time, "sleep", lambda _: None)

    ready, fresh, current_health = manual_rtsp._wait_for_ready(
        FakeCamera(), FakeBuffer(), timeout_s=1.0
    )

    assert ready is True
    assert fresh == ["fresh-frame"]
    assert current_health.stale is False


def test_manual_runtime_saves_prepared_crop_without_bbox_reconstruction():
    source = Path(manual_rtsp.__file__).read_text(encoding="utf-8")

    assert "pipeline.execute_packets(" in source
    assert "execution.label_crop_snapshot" in source
    assert "crop_image(" not in source
