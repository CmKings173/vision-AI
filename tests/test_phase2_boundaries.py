import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import (
    BusinessStatus,
    ProcessingStatus,
    TriggerEvent,
)
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.extraction.fields import FieldExtractor
from label_inspection.pipeline.inspection import InspectionPipeline
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import BarcodeResult, FramePacket, OCRLine, RawOCRResult
from label_inspection.station.preparation import StationPreparer
from label_inspection.validation.rules import LabelValidator
from label_inspection.worker.processor import InspectionProcessor
from tests.fixtures.quality import sharp_label

pytestmark = pytest.mark.integration


def _trigger() -> TriggerEvent:
    return TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=int(time.time() * 1000),
    )


def _packet(frame, *, frame_id=7, age_ms=100) -> FramePacket:
    return FramePacket(
        frame_id=frame_id,
        captured_at=time.time() - age_ms / 1000.0,
        frame=frame,
        source="rtsp",
        captured_monotonic=time.monotonic() - age_ms / 1000.0,
    )


def _quality_pass() -> QualityChecker:
    return QualityChecker(
        min_width=1,
        min_height=1,
        min_brightness=0,
        max_brightness=255,
        min_sharpness=0,
        max_underexposed_ratio=1,
        max_overexposed_ratio=1,
        max_glare_ratio=1,
    )


def _preparer(*, detector=None, quality_checker=None, rotate_degrees=0):
    return StationPreparer(
        detector=detector or FixedROIDetector((0.1, 0.1, 0.9, 0.9)),
        selector=FrameSelector(top_k=1, score_fn=lambda frame: 1.0),
        quality_checker=quality_checker or _quality_pass(),
        candidate_scorer=CandidateScorer(sharpness_reference=500),
        station_id="STATION-01",
        camera_id="PHONE-01",
        rotate_degrees=rotate_degrees,
        bbox_padding_ratio=0.0,
    )


def test_station_preparation_owns_orientation_fixed_roi_and_exact_crop_pixels():
    frame = np.arange(40 * 60 * 3, dtype=np.uint8).reshape((40, 60, 3))
    preparer = _preparer(
        detector=FixedROIDetector((0.0, 0.0, 1.0, 1.0)),
        rotate_degrees=90,
    )

    outcome = preparer.prepare_trigger([_packet(frame)], trigger=_trigger())
    prepared = outcome.prepared
    expected = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

    assert outcome.processing_status is ProcessingStatus.PREPARED
    assert outcome.business_status is None
    assert outcome.inference_required is True
    assert prepared is not None
    assert prepared.orientation_degrees == 90
    np.testing.assert_array_equal(prepared.selected_frame, expected)
    np.testing.assert_array_equal(prepared.label_crop, expected)
    assert not np.shares_memory(prepared.selected_frame, frame)
    assert not np.shares_memory(prepared.label_crop, prepared.selected_frame)
    assert prepared.received_at_ms <= prepared.prepared_at_ms
    assert prepared.source_timestamp_ms is None


def test_station_preparation_exposes_selected_detector_inputs_and_attempts():
    preparer = _preparer(detector=FixedROIDetector((0.1, 0.1, 0.9, 0.9)))
    packet = _packet(sharp_label(), frame_id=12)

    outcome = preparer.prepare_trigger([packet], trigger=_trigger())

    assert outcome.prepared is not None
    assert preparer.last_debug["selected_frame_ids"] == [12]
    assert preparer.last_debug["detector_input_frame_ids"] == [12]
    assert preparer.last_debug["detector_attempts"][0]["event_frame_id"] == 12
    assert preparer.last_debug["accepted_candidates"][0]["frame_id"] == 12


class IdentityOCR:
    engine = "fake-ppocr"

    def __init__(self):
        self.image = None

    def recognize(self, image):
        self.image = image
        return RawOCRResult(
            engine=self.engine,
            lines=[OCRLine(text="SKU: ABC123", confidence=0.99)],
        )


class IdentityBarcode:
    def __init__(self):
        self.image = None

    def decode(self, image):
        self.image = image
        return [BarcodeResult(value="DM-001", format="DataMatrix", valid=True)]


class ConcurrentInferenceProbe:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2, timeout=0.5)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.barrier.wait()
        finally:
            with self.lock:
                self.active -= 1


class ConcurrentOCR:
    engine = "fake-ppocr"

    def __init__(self, probe: ConcurrentInferenceProbe):
        self.probe = probe
        self.thread_id = None

    def recognize(self, image):
        self.thread_id = threading.get_ident()
        self.probe.enter()
        return RawOCRResult(
            engine=self.engine,
            lines=[OCRLine(text="SKU: ABC123", confidence=0.99)],
        )


class ConcurrentBarcode:
    def __init__(self, probe: ConcurrentInferenceProbe):
        self.probe = probe

    def decode(self, image):
        self.probe.enter()
        return [BarcodeResult(value="DM-001", format="DataMatrix", valid=True)]


def test_worker_processor_runs_ocr_and_barcode_concurrently():
    outcome = _preparer().prepare_trigger([_packet(sharp_label())], trigger=_trigger())
    assert outcome.prepared is not None
    probe = ConcurrentInferenceProbe()
    ocr = ConcurrentOCR(probe)
    caller_thread_id = threading.get_ident()
    processor = InspectionProcessor(
        ocr=ocr,
        barcode=ConcurrentBarcode(probe),
        extractor=FieldExtractor(fields=("sku",)),
        validator=LabelValidator(
            required_fields=("sku",),
            profile_name="test-profile",
            profile_version="1.0",
            profile_approved=True,
        ),
    )

    result = processor.process(outcome.prepared)

    assert probe.max_active == 2
    assert ocr.thread_id == caller_thread_id
    assert result.timing["parallel_inference_ms"] >= max(
        result.timing["ocr_ms"], result.timing["barcode_ms"]
    )
    assert result.raw_ocr.success is True
    assert result.barcode.value == "DM-001"
    assert result.extracted["sku"].value == "ABC123"


def test_worker_processor_consumes_the_exact_prepared_crop_without_reconstruction():
    outcome = _preparer().prepare_trigger([_packet(sharp_label())], trigger=_trigger())
    assert outcome.prepared is not None
    ocr = IdentityOCR()
    barcode = IdentityBarcode()
    processor = InspectionProcessor(
        ocr=ocr,
        barcode=barcode,
        extractor=FieldExtractor(fields=("sku",)),
        validator=LabelValidator(
            required_fields=("sku",),
            profile_name="test-profile",
            profile_version="1.0",
            profile_approved=True,
        ),
    )

    result = processor.process(outcome.prepared)

    assert ocr.image is outcome.prepared.label_crop
    assert barcode.image is outcome.prepared.label_crop
    assert result.event_id == outcome.prepared.event_id
    assert result.extracted["sku"].value == "ABC123"


def test_pipeline_execution_exposes_pre_inference_crop_snapshot():
    ocr = IdentityOCR()
    barcode = IdentityBarcode()
    pipeline = InspectionPipeline(
        preparer=_preparer(),
        processor=InspectionProcessor(
            ocr=ocr,
            barcode=barcode,
            extractor=FieldExtractor(fields=("sku",)),
        validator=LabelValidator(
            required_fields=("sku",),
            profile_name="test-profile",
            profile_version="1.0",
            profile_approved=True,
        ),
        ),
    )

    execution = pipeline.execute_packets([_packet(sharp_label())])

    assert execution.prepared is not None
    assert execution.label_crop_snapshot is not None
    assert execution.label_crop_snapshot is not ocr.image
    assert np.array_equal(execution.label_crop_snapshot, ocr.image)
    assert ocr.image is execution.prepared.label_crop
    assert barcode.image is execution.prepared.label_crop


def test_quality_rejection_is_terminal_and_never_requires_worker_inference():
    preparer = _preparer(
        quality_checker=QualityChecker(min_sharpness=1_000_000_000)
    )

    outcome = preparer.prepare_trigger([_packet(sharp_label())], trigger=_trigger())
    terminal = outcome.to_terminal_result()

    assert outcome.processing_status is ProcessingStatus.COMPLETED
    assert outcome.business_status is BusinessStatus.REVIEW
    assert outcome.inference_required is False
    assert outcome.prepared is not None
    assert terminal.processing_status is ProcessingStatus.COMPLETED
    assert terminal.business_status is BusinessStatus.REVIEW
    assert terminal.inference_executed is False
    assert "QUALITY_REJECTED" in terminal.reasons


def test_no_frame_is_terminal_technical_error_not_business_fail():
    trigger = _trigger()

    outcome = _preparer().prepare_trigger([], trigger=trigger)
    terminal = outcome.to_terminal_result()

    assert outcome.processing_status is ProcessingStatus.ERROR
    assert outcome.business_status is None
    assert outcome.inference_required is False
    assert outcome.prepared is None
    assert terminal.business_status is None
    assert terminal.error is not None
    assert terminal.error.code == "NO_FRAME_CANDIDATE"


def test_station_factory_does_not_import_or_validate_ocr_barcode_runtime():
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "src"
    code = """
import sys
from dataclasses import replace
from label_inspection.app import build_station_preparer
from label_inspection.config import Settings

config = replace(
    Settings(),
    detector="fixed-roi",
    label_roi="0.1,0.1,0.9,0.9",
    roi_normalized=True,
    ocr_engine="not-a-station-concern",
    barcode_engine="not-a-station-concern",
)
build_station_preparer(config)
forbidden = {
    "label_inspection.ocr.ppocr",
    "label_inspection.ocr.ppocr_v6",
    "label_inspection.ocr.tensorrt_ocr",
    "label_inspection.barcode.zxing",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("station imported inference runtime: " + ",".join(loaded))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_worker_processor_module_does_not_import_camera_runtime():
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "src"
    code = """
import sys
from label_inspection.worker.processor import InspectionProcessor
forbidden = {
    "label_inspection.camera.rtsp",
    "label_inspection.camera.acquisition",
    "label_inspection.camera.frame_buffer",
    "label_inspection.camera.selector",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit("worker imported camera runtime: " + ",".join(loaded))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root)

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
