import json
import time

import pytest

from label_inspection.camera.frame_buffer import FrameBuffer
from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import DeliveryStatus
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.extraction.fields import FieldExtractor
from label_inspection.messaging import (
    FrozenJobPublisher,
    RetryingWorkerMessageHandler,
)
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import (
    BarcodeResult,
    FramePacket,
    OCRLine,
    RawOCRResult,
)
from label_inspection.station.controller import StationController
from label_inspection.station.dispatcher import OutboxDispatcher
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.spool import LocalSpool
from label_inspection.storage import InMemoryArtifactStore
from label_inspection.validation.rules import LabelValidator
from label_inspection.worker import InferenceWorker, InspectionProcessor
from tests.fixtures.quality import sharp_label

pytestmark = pytest.mark.integration


class _Warmup:
    success = True


class _ResidentOCR:
    engine = "fake-ppocr-v6"

    def __init__(self) -> None:
        self.ready = False
        self.calls = 0

    def warmup(self):
        self.ready = True
        return _Warmup()

    def recognize(self, image):
        self.calls += 1
        return RawOCRResult(
            engine=self.engine,
            lines=[OCRLine(text="SKU: PHASE2-E2E", confidence=0.99)],
        )


class _ResidentBarcode:
    def prepare(self):
        return True

    def decode(self, image):
        return [
            BarcodeResult(
                value="DM-PHASE2",
                format="DataMatrix",
                confidence=1.0,
                valid=True,
            )
        ]


class _CapturePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, **kwargs):
        self.messages.append(kwargs)


class _NoopRetryPublisher:
    def publish(self, **kwargs):
        raise AssertionError("successful E2E processing must not retry")


def test_station_to_worker_local_contract_integration(tmp_path):
    frame_buffer = FrameBuffer(max_size=4, window_ms=1000)
    frame_buffer.append(
        FramePacket(
            frame_id=101,
            captured_at=time.time() - 0.01,
            captured_monotonic=time.monotonic(),
            frame=sharp_label(),
            source="rtsp",
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
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    controller = StationController(
        frame_buffer=frame_buffer,
        preparer=preparer,
        spool=spool,
        station_id="STATION-01",
        camera_id="PHONE-01",
        provenance={
            "requested_profile": {"name": "default", "version": "1.0"},
            "producer": {"locator_version": "fixed-roi.v1"},
        },
    )

    local = controller.trigger()
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    OutboxDispatcher(spool=spool, store=store).dispatch_pending()
    capture = _CapturePublisher()
    FrozenJobPublisher(spool=spool, publisher=capture).publish_pending()

    assert (
        spool.open_record(local.event_id).state.delivery_status
        is DeliveryStatus.JOB_PUBLISHED
    )
    assert capture.messages[0]["body"] == local.record.frozen_job_bytes()

    ocr = _ResidentOCR()
    processor = InspectionProcessor(
        ocr=ocr,
        barcode=_ResidentBarcode(),
        extractor=FieldExtractor(fields=("sku",)),
        validator=LabelValidator(
            required_fields=("sku",),
            profile_name="test-profile",
            profile_version="1.0",
            profile_approved=True,
        ),
    )
    worker = InferenceWorker(processor=processor, store=store)
    worker.start()
    acked = []
    disposition = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=_NoopRetryPublisher(),
    ).handle(
        capture.messages[0]["body"],
        message_id=local.event_id,
        headers={"attempt": 0},
        ack=lambda: acked.append(True),
    )

    assert disposition.value == "COMPLETED"
    assert acked == [True]
    assert ocr.calls == 1
    result_key = local.record.job.artifacts["label_crop"].key.replace(
        "/source/label_crop.png", "/result/result.json"
    )
    metadata = store.head("vision-inspections", result_key)
    assert metadata is not None
    result = json.loads(
        store.get_verified(
            type(local.record.job.artifacts["label_crop"])(
                bucket=metadata.bucket,
                key=metadata.key,
                sha256=metadata.sha256,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
            )
        )
    )
    assert result["business_status"] == "PASS"
    assert result["result_payload"]["inspection"]["extracted"]["sku"]["value"] == "PHASE2-E2E"
    assert result["result_payload"]["inspection"]["barcode"]["format"] == "DataMatrix"
