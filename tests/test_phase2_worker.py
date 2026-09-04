import hashlib
import json
import struct
import time
import uuid
from dataclasses import replace

import cv2
import numpy as np
import pytest

from label_inspection.camera.selector import FrameSelector
from label_inspection.contracts import (
    APPROVED_FOR_AUTOMATED_PASS,
    DocumentRecognitionResult,
    ProfileBinding,
    TriggerEvent,
)
from label_inspection.detection.fixed_roi import FixedROIDetector
from label_inspection.extraction.fields import FieldExtractor
from label_inspection.messaging import (
    DeliveryDisposition,
    MessagePublishError,
    RetryingWorkerMessageHandler,
)
from label_inspection.pipeline.ranking import CandidateScorer
from label_inspection.preprocessing.quality import QualityChecker
from label_inspection.schemas import (
    BarcodeResult,
    ExtractedField,
    FramePacket,
    InspectionResult,
    OCRLine,
    QualityReport,
    RawOCRResult,
    ValidationResult,
)
from label_inspection.station.dispatcher import OutboxDispatcher
from label_inspection.station.preparation import StationPreparer
from label_inspection.station.spool import LocalSpool
from label_inspection.storage import (
    ArtifactPolicyError,
    InMemoryArtifactStore,
    ObjectMetadata,
    StorageError,
    event_object_keys,
)
from label_inspection.validation.rules import LabelValidator
from label_inspection.worker.inference_worker import (
    InferenceWorker,
    WorkerContractError,
    WorkerResultConflictError,
)
from label_inspection.worker.processor import InspectionProcessor
from label_inspection.worker.provenance import WorkerRuntimeDescriptor
from tests.fixtures.quality import sharp_label


class _Warmup:
    success = True


class _OCR:
    engine = "fake-ppocr-v6"

    def __init__(self) -> None:
        self.ready = False
        self.warmup_calls = 0
        self.recognize_calls = 0
        self.images = []

    def warmup(self):
        self.warmup_calls += 1
        self.ready = True
        return _Warmup()

    def recognize(self, image):
        self.recognize_calls += 1
        self.images.append(image.copy())
        return RawOCRResult(
            engine=self.engine,
            lines=[OCRLine(text="SKU: ABC123", confidence=0.99)],
        )


class _Barcode:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.decode_calls = 0

    def prepare(self):
        self.prepare_calls += 1
        return True

    def decode(self, image):
        self.decode_calls += 1
        return [
            BarcodeResult(
                value="DM-001", format="DataMatrix", valid=True, confidence=1.0
            )
        ]


class _RaisingOCR(_OCR):
    def recognize(self, image):
        self.recognize_calls += 1
        raise RuntimeError("simulated OCR runtime failure")


class _RaisingBarcode(_Barcode):
    def decode(self, image):
        self.decode_calls += 1
        raise RuntimeError("simulated barcode runtime failure")


class _NoBarcode(_Barcode):
    def decode(self, image):
        self.decode_calls += 1
        return []


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = []
        self.fail = fail

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise MessagePublishError("simulated publish confirm failure")


def _job(tmp_path):
    trigger = TriggerEvent.create(
        station_id="STATION-01",
        camera_id="PHONE-01",
        triggered_at_ms=int(time.time() * 1000),
    )
    packet = FramePacket(
        frame_id=23,
        captured_at=time.time() - 0.1,
        frame=sharp_label(),
        source="rtsp",
        captured_monotonic=time.monotonic() - 0.1,
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
        station_id=trigger.station_id,
        camera_id=trigger.camera_id,
        bbox_padding_ratio=0,
    )
    outcome = preparer.prepare_trigger([packet], trigger=trigger)
    spool = LocalSpool(tmp_path / "spool", bucket="vision-inspections")
    record = spool.commit_outcome(
        outcome,
        provenance={
            "requested_profile": {"name": "test-profile", "version": "1.0"},
            "producer": {"locator_version": "fixed-roi.v1"},
        },
    )
    store = InMemoryArtifactStore()
    store.ensure_bucket("vision-inspections")
    OutboxDispatcher(spool=spool, store=store).dispatch_record(record)
    return record.job, record.frozen_job_bytes(), store


def _worker(
    store,
    ocr=None,
    barcode=None,
    *,
    recognition_status="KNOWN",
):
    ocr = ocr or _OCR()
    barcode = barcode or _Barcode()
    profile_binding = ProfileBinding(
        name="test-profile",
        version="1.0",
        approval_status=APPROVED_FOR_AUTOMATED_PASS,
    )
    processor = InspectionProcessor(
        ocr=ocr,
        barcode=barcode,
        extractor=FieldExtractor(
            fields=("sku",),
            profile_binding=profile_binding,
        ),
        validator=LabelValidator(
            required_fields=("sku",),
            profile_name="test-profile",
            profile_version="1.0",
            profile_approved=True,
        ),
        document_recognition=(
            None
            if recognition_status is None
            else DocumentRecognitionResult(
                status=recognition_status,
                profile_binding=profile_binding,
            )
        ),
    )
    return InferenceWorker(processor=processor, store=store), ocr, barcode


class _ObservedStore(InMemoryArtifactStore):
    def __init__(self, source: InMemoryArtifactStore) -> None:
        super().__init__()
        self._buckets = source._buckets.copy()
        self._objects = source._objects.copy()
        self.head_calls = 0
        self.get_calls = 0
        self.put_calls = 0
        self.ensure_calls = 0

    def ensure_bucket(self, bucket):
        self.ensure_calls += 1
        return super().ensure_bucket(bucket)

    def head(self, bucket, key):
        self.head_calls += 1
        return super().head(bucket, key)

    def get_verified(self, reference, *, max_bytes=None):
        self.get_calls += 1
        return super().get_verified(reference, max_bytes=max_bytes)

    def put_if_absent(self, reference, content):
        self.put_calls += 1
        return super().put_if_absent(reference, content)


def _mutated_job_body(body: bytes, attack: str) -> bytes:
    payload = json.loads(body)
    crop = payload["artifacts"]["label_crop"]
    if attack == "other_bucket":
        crop["bucket"] = "attacker-bucket"
    elif attack == "unrelated_key":
        crop["key"] = "STATION-01/2026/01/01/unrelated/source/label_crop.png"
    elif attack == "another_event":
        crop["key"] = crop["key"].replace(payload["event_id"], str(uuid.uuid4()))
    elif attack == "traversal_key":
        crop["key"] = "../unrelated/source/label_crop.png"
    elif attack == "wrong_content_type":
        crop["content_type"] = "application/octet-stream"
    elif attack == "oversized":
        crop["size_bytes"] = 16 * 1024 * 1024 + 1
    elif attack == "malformed_checksum":
        crop["sha256"] = "not-a-sha256"
    elif attack == "result_destination":
        payload["artifacts"]["result"] = {
            **crop,
            "bucket": "attacker-bucket",
            "key": "attacker/result.json",
            "content_type": "application/json",
        }
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown attack: {attack}")
    return json.dumps(payload).encode("utf-8")


@pytest.mark.parametrize(
    "attack",
    [
        "other_bucket",
        "unrelated_key",
        "another_event",
        "traversal_key",
        "wrong_content_type",
        "oversized",
        "malformed_checksum",
        "result_destination",
    ],
)
def test_worker_rejects_untrusted_artifact_location_before_storage_or_ocr(
    tmp_path, attack
):
    _, body, source_store = _job(tmp_path)
    store = _ObservedStore(source_store)
    worker, ocr, _ = _worker(store)
    worker.start()

    with pytest.raises((ArtifactPolicyError, WorkerContractError)):
        worker.process_message(_mutated_job_body(body, attack))

    assert store.head_calls == 0
    assert store.get_calls == 0
    assert store.put_calls == 0
    assert ocr.recognize_calls == 0


def test_worker_is_ready_only_after_one_warmup_and_reuses_resident_models(tmp_path):
    job, body, store = _job(tmp_path)
    worker, ocr, barcode = _worker(store)
    assert worker.ready is False

    worker.start()
    first = worker.process_message(body)
    second = worker.process_message(body)

    assert worker.ready is True
    assert ocr.warmup_calls == 1
    assert barcode.prepare_calls == 1
    assert ocr.recognize_calls == 1
    assert first.durable_result is True
    assert first.queue_wait_ms >= 0
    assert first.checksum_ms >= 0
    assert first.end_to_end_ms >= 0
    assert second.inference_skipped is True
    crop_bytes = store.get_verified(job.artifacts["label_crop"])
    expected = cv2.imdecode(np.frombuffer(crop_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    np.testing.assert_array_equal(ocr.images[0], expected)


def test_manual_ack_occurs_only_after_result_is_durable(tmp_path):
    job, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()
    observations = []

    def ack():
        key = event_object_keys(
            station_id=job.station_id,
            event_id=job.event_id,
            occurred_at_ms=job.triggered_at_ms,
        ).result
        observations.append(store.head("vision-inspections", key) is not None)

    RetryingWorkerMessageHandler(
        worker=worker, publisher=_Transport()
    ).handle(
        body,
        message_id=job.event_id,
        correlation_id=job.event_id,
        headers={},
        ack=ack,
    )

    assert observations == [True]


def test_result_persist_failure_never_acks_delivery(tmp_path):
    job, body, base_store = _job(tmp_path)

    class FailingResultStore(InMemoryArtifactStore):
        def put_if_absent(self, reference, content):
            if reference.key.endswith("/result/result.json"):
                raise StorageError("simulated durable result outage")
            return super().put_if_absent(reference, content)

    store = FailingResultStore()
    store._buckets = base_store._buckets.copy()
    store._objects = base_store._objects.copy()
    worker, _, _ = _worker(store)
    worker.start()
    acked = []

    with pytest.raises(MessagePublishError):
        RetryingWorkerMessageHandler(
            worker=worker,
            publisher=_Transport(fail=True),
        ).handle(
            body,
            message_id=job.event_id,
            correlation_id=job.event_id,
            headers={},
            ack=lambda: acked.append(True),
        )

    assert acked == []


def test_ocr_runtime_failure_is_durable_error_and_not_dead_lettered(tmp_path):
    job, body, store = _job(tmp_path)
    worker, _, _ = _worker(store, ocr=_RaisingOCR())
    worker.start()
    acked = []
    transport = _Transport()

    disposition = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=transport,
    ).handle(
        body,
        message_id=job.event_id,
        correlation_id=job.event_id,
        headers={},
        ack=lambda: acked.append(True),
    )

    assert disposition is DeliveryDisposition.COMPLETED
    assert acked == [True]
    assert all(
        call["routing_key"] != "inspection.dead"
        for call in transport.calls
    )
    result_key = event_object_keys(
        station_id=job.station_id,
        event_id=job.event_id,
        occurred_at_ms=job.triggered_at_ms,
    ).result
    metadata = store.head("vision-inspections", result_key)
    assert metadata is not None
    payload = json.loads(
        store.get_verified(
            type(job.artifacts["label_crop"])(
                bucket=metadata.bucket,
                key=metadata.key,
                sha256=metadata.sha256,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
            )
        )
    )
    assert payload["processing_status"] == "ERROR"
    assert payload["business_status"] is None
    assert payload["error"]["code"] == "OCR_RUNTIME_ERROR"


def test_barcode_runtime_failure_is_durable_error(tmp_path):
    _job_record, body, store = _job(tmp_path)
    worker, _, _ = _worker(store, barcode=_RaisingBarcode())
    worker.start()

    report = worker.process_message(body)

    assert report.durable_result is True
    assert report.result.processing_status.value == "ERROR"
    assert report.result.business_status is None
    assert report.result.error is not None
    assert report.result.error.code == "BARCODE_RUNTIME_ERROR"


def test_unexpected_processor_runtime_failure_is_durable_error(tmp_path):
    _job_record, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()

    def raise_processing_failure(_prepared):
        raise RuntimeError("unexpected processing failure")

    worker.processor.process = raise_processing_failure

    report = worker.process_message(body)

    assert report.durable_result is True
    assert report.result.processing_status.value == "ERROR"
    assert report.result.business_status is None
    assert report.result.error is not None
    assert report.result.error.code == "PROCESSING_RUNTIME_ERROR"


def test_invalid_processor_status_is_durable_error_not_dead_lettered(tmp_path):
    job, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()

    worker.processor.process = lambda _prepared: InspectionResult(
        event_id=job.event_id,
        camera_id=job.camera_id,
        raw_ocr=RawOCRResult(engine="test"),
        barcode=BarcodeResult(value=None),
        quality=QualityReport(status="PASS"),
        validation=ValidationResult(status="NOT_A_BUSINESS_STATUS"),
    )

    report = worker.process_message(body)

    assert report.result.processing_status.value == "ERROR"
    assert report.result.business_status is None
    assert report.result.error is not None
    assert report.result.error.code == "PROCESSOR_INVALID_STATUS"


def test_malformed_processor_result_is_durable_error_not_dead_lettered(tmp_path):
    job, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()
    worker.processor.process = lambda _prepared: object()

    acked = []
    transport = _Transport()
    disposition = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=transport,
    ).handle(
        body,
        message_id=job.event_id,
        correlation_id=job.event_id,
        headers={},
        ack=lambda: acked.append(True),
    )

    assert disposition is DeliveryDisposition.COMPLETED
    assert acked == [True]
    assert all(call["routing_key"] != "inspection.dead" for call in transport.calls)


def test_image_decode_runtime_failure_is_durable_error_not_retry_or_dlq(
    tmp_path, monkeypatch
):
    job, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()

    def raise_decode_failure(*_args, **_kwargs):
        raise RuntimeError("decoder library failure")

    monkeypatch.setattr(
        "label_inspection.worker.inference_worker.cv2.imdecode",
        raise_decode_failure,
    )
    acked = []
    transport = _Transport()

    disposition = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=transport,
    ).handle(
        body,
        message_id=job.event_id,
        correlation_id=job.event_id,
        headers={},
        ack=lambda: acked.append(True),
    )

    assert disposition is DeliveryDisposition.COMPLETED
    assert acked == [True]
    assert all(call["routing_key"] != "inspection.dead" for call in transport.calls)
    result_key = event_object_keys(
        station_id=job.station_id,
        event_id=job.event_id,
        occurred_at_ms=job.triggered_at_ms,
    ).result
    metadata = store.head("vision-inspections", result_key)
    assert metadata is not None
    result_payload = json.loads(
        store.get_verified(
            type(job.artifacts["label_crop"])(
                bucket=metadata.bucket,
                key=metadata.key,
                sha256=metadata.sha256,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
            )
        )
    )
    assert result_payload["processing_status"] == "ERROR"
    assert result_payload["business_status"] is None
    assert result_payload["inference_executed"] is False
    assert result_payload["error"]["code"] == "IMAGE_DECODE_RUNTIME_ERROR"


def test_successful_barcode_decode_with_no_value_is_not_a_runtime_error(tmp_path):
    _job_record, body, store = _job(tmp_path)
    worker, _, barcode = _worker(store, barcode=_NoBarcode())
    worker.start()

    report = worker.process_message(body)

    assert barcode.decode_calls == 1
    assert report.result.processing_status.value == "COMPLETED"
    assert report.result.business_status is not None
    assert report.result.result_payload["inspection"]["barcode"]["value"] is None


@pytest.mark.parametrize("recognition_status", [None, "UNKNOWN", "AMBIGUOUS"])
def test_requested_profile_without_known_recognition_cannot_pass(
    tmp_path, recognition_status,
):
    _job_record, body, store = _job(tmp_path)
    worker, _, _ = _worker(store, recognition_status=recognition_status)
    worker.start()

    report = worker.process_message(body)

    assert report.result.processing_status.value == "COMPLETED"
    assert report.result.business_status.value == "REVIEW"
    assert "NO_TRUSTED_DOCUMENT_RECOGNITION" in report.result.reasons


def test_unapproved_custom_validator_cannot_publish_pass(tmp_path):
    _job_record, body, source_store = _job(tmp_path)
    payload = json.loads(body)
    payload["provenance"]["requested_profile"] = None
    store = _ObservedStore(source_store)
    processor = InspectionProcessor(ocr=_OCR(), barcode=_Barcode())
    processor.validator.validate = lambda *_args: ValidationResult(status="PASS")
    worker = InferenceWorker(processor=processor, store=store)
    worker.start()

    report = worker.process_message(json.dumps(payload).encode("utf-8"))

    assert report.result.processing_status.value == "COMPLETED"
    assert report.result.business_status.value == "REVIEW"
    assert "NO_APPROVED_PROFILE" in report.result.reasons
    assert report.result.result_payload["inspection"]["extracted"] == {}


@pytest.mark.parametrize("forged_status", ["PASS", "FAIL"])
def test_worker_boundary_blocks_unauthorized_business_decisions_and_fields(
    tmp_path, forged_status,
):
    job, body, source_store = _job(tmp_path)
    payload = json.loads(body)
    payload["provenance"]["requested_profile"] = None
    store = _ObservedStore(source_store)
    processor = InspectionProcessor(ocr=_OCR(), barcode=_Barcode())
    processor.process = lambda _prepared: InspectionResult(
        event_id=job.event_id,
        camera_id=job.camera_id,
        raw_ocr=RawOCRResult(engine="test"),
        extracted={
            "invented_business_field": ExtractedField(
                value="invented",
                confidence=1.0,
                source="test",
            )
        },
        barcode=BarcodeResult(value="DM-001"),
        quality=QualityReport(status="PASS"),
        validation=ValidationResult(
            status=forged_status,
            reasons=("FORGED_SEMANTIC_REASON",),
        ),
    )
    worker = InferenceWorker(processor=processor, store=store)
    worker.start()

    report = worker.process_message(json.dumps(payload).encode("utf-8"))

    assert report.result.processing_status.value == "COMPLETED"
    assert report.result.business_status.value == "REVIEW"
    assert report.result.reasons == ("NO_APPROVED_PROFILE",)
    inspection = report.result.result_payload["inspection"]
    assert inspection["extracted"] == {}
    assert inspection["validation"] == {
        "status": "REVIEW",
        "reasons": ("NO_APPROVED_PROFILE",),
    }


def test_worker_boundary_clears_unauthorized_fields_from_technical_error(
    tmp_path,
):
    job, body, source_store = _job(tmp_path)
    payload = json.loads(body)
    payload["provenance"]["requested_profile"] = None
    store = _ObservedStore(source_store)
    processor = InspectionProcessor(ocr=_OCR(), barcode=_Barcode())
    processor.process = lambda _prepared: InspectionResult(
        event_id=job.event_id,
        camera_id=job.camera_id,
        raw_ocr=RawOCRResult(
            engine="test",
            success=False,
            error_code="OCR_RUNTIME_ERROR",
        ),
        extracted={
            "invented_business_field": ExtractedField(
                value="invented",
                confidence=1.0,
                source="test",
            )
        },
        barcode=BarcodeResult(value=None),
        quality=QualityReport(status="PASS"),
        validation=ValidationResult(
            status="ERROR",
            reasons=("OCR_RUNTIME_ERROR",),
        ),
    )
    worker = InferenceWorker(processor=processor, store=store)
    worker.start()

    report = worker.process_message(json.dumps(payload).encode("utf-8"))

    assert report.result.processing_status.value == "ERROR"
    assert report.result.result_payload["inspection"]["extracted"] == {}


def test_worker_detects_runtime_descriptor_drift_before_inference(tmp_path):
    _job_record, body, store = _job(tmp_path)
    worker, ocr, barcode = _worker(store)
    worker.start()
    worker.processor._document_recognition = None

    report = worker.process_message(body)

    assert report.result.processing_status.value == "ERROR"
    assert report.result.business_status is None
    assert report.result.inference_executed is False
    assert report.result.error is not None
    assert report.result.error.code == "WORKER_RUNTIME_DRIFT"
    assert ocr.recognize_calls == 0
    assert barcode.decode_calls == 0


def test_profile_free_job_preserves_evidence_and_is_durable_review(tmp_path):
    _job_record, body, source_store = _job(tmp_path)
    payload = json.loads(body)
    payload["provenance"]["requested_profile"] = None
    store = _ObservedStore(source_store)
    processor = InspectionProcessor(ocr=_OCR(), barcode=_Barcode())
    worker = InferenceWorker(processor=processor, store=store)
    worker.start()

    report = worker.process_message(json.dumps(payload).encode("utf-8"))

    assert report.durable_result is True
    assert report.result.business_status.value == "REVIEW"
    inspection = report.result.result_payload["inspection"]
    assert inspection["extracted"] == {}
    assert inspection["evidence"]


def test_existing_unauthorized_pass_is_rejected_as_durable_conflict(tmp_path):
    job, body, source_store = _job(tmp_path)
    request = json.loads(body)
    request["provenance"]["requested_profile"] = None
    body = json.dumps(request).encode("utf-8")
    store = _ObservedStore(source_store)
    worker = InferenceWorker(
        processor=InspectionProcessor(ocr=_OCR(), barcode=_Barcode()),
        store=store,
    )
    worker.start()
    first = worker.process_message(body)
    payload = json.loads(store.get_verified(first.result_reference))
    payload["business_status"] = "PASS"
    forged_bytes = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = (first.result_reference.bucket, first.result_reference.key)
    store._objects[identity] = (
        ObjectMetadata(
            bucket=first.result_reference.bucket,
            key=first.result_reference.key,
            sha256=hashlib.sha256(forged_bytes).hexdigest(),
            size_bytes=len(forged_bytes),
            content_type="application/json",
        ),
        forged_bytes,
    )

    with pytest.raises(WorkerResultConflictError, match="semantic authorization"):
        worker.process_message(body)


def test_runtime_descriptor_mismatch_is_rejected_at_worker_init(tmp_path):
    _, _, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    actual = WorkerRuntimeDescriptor.from_processor(worker.processor)
    forged = replace(actual, ocr={**actual.ocr, "implementation": "OtherOCR"})

    with pytest.raises(ValueError, match="runtime descriptor"):
        InferenceWorker(
            processor=worker.processor,
            store=store,
            runtime_descriptor=forged,
        )


def test_checksum_failure_never_runs_inference_and_acks_only_after_dlq(tmp_path):
    job, body, store = _job(tmp_path)
    identity = (job.artifacts["label_crop"].bucket, job.artifacts["label_crop"].key)
    metadata, _ = store._objects[identity]
    store._objects[identity] = (metadata, b"tampered")
    worker, ocr, _ = _worker(store)
    worker.start()
    acked = []

    transport = _Transport()
    disposition = RetryingWorkerMessageHandler(
        worker=worker,
        publisher=transport,
    ).handle(
        body,
        message_id=job.event_id,
        correlation_id=job.event_id,
        headers={},
        ack=lambda: acked.append(True),
    )

    assert ocr.recognize_calls == 0
    assert disposition is DeliveryDisposition.DEAD_LETTER
    assert transport.calls[0]["routing_key"].endswith("dead")
    assert acked == [True]


def test_durable_result_keeps_business_payload_and_semantic_provenance(tmp_path):
    job, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()

    report = worker.process_message(body)
    payload = json.loads(store.get_verified(report.result_reference))

    assert payload["event_id"] == job.event_id
    assert payload["business_status"] == "PASS"
    assert payload["reasons"] == []
    assert payload["inference_executed"] is True
    assert (
        payload["result_payload"]["inspection"]["extracted"]["sku"]["value"]
        == "ABC123"
    )
    assert payload["result_payload"]["producer_provenance"] == dict(job.provenance)
    worker_runtime = payload["result_payload"]["worker_runtime_provenance"]
    assert worker_runtime["pipeline_version"] == "phase2-worker.v2"
    assert worker_runtime["trusted_document_recognition"] is True
    assert worker_runtime["document_recognition"] == {
        "status": "KNOWN",
        "profile_binding": {
            "name": "test_profile",
            "version": "1.0",
            "approval_status": "APPROVED_FOR_AUTOMATED_PASS",
        },
        "reason": None,
    }
    assert worker_runtime["profile"] == {
        "name": "test_profile",
        "version": "1.0",
        "approval_status": "APPROVED_FOR_AUTOMATED_PASS",
    }
    assert worker_runtime["ocr"]["implementation"] == "_OCR"
    assert worker_runtime["barcode"]["implementation"] == "_Barcode"
    assert worker_runtime["extractor"]["profile_name"] == "test_profile"
    assert worker_runtime["extractor"]["profile_version"] == "1.0"
    assert worker_runtime["validator"]["implementation"] == "LabelValidator"
    timings = payload["result_payload"]["stage_timings"]
    assert timings["queue_wait_ms"] >= 0
    assert timings["artifact_download_ms"] >= 0
    assert timings["checksum_ms"] >= 0
    assert timings["ocr_ms"] >= 0


def test_requested_profile_mismatch_is_nonretryable_before_storage_or_inference(
    tmp_path
):
    _, body, source_store = _job(tmp_path)
    payload = json.loads(body)
    payload["provenance"]["requested_profile"] = {
        "name": "dgx_spark_label",
        "version": "1.0",
    }
    store = _ObservedStore(source_store)
    worker, ocr, barcode = _worker(store)
    worker.start()

    with pytest.raises(Exception) as raised:
        worker.process_message(json.dumps(payload).encode("utf-8"))

    assert getattr(raised.value, "code", None) == "PROFILE_MISMATCH"
    assert getattr(raised.value, "retryable", True) is False
    assert store.head_calls == 0
    assert store.get_calls == 0
    assert ocr.recognize_calls == 0
    assert barcode.decode_calls == 0


def test_png_pixel_limit_is_enforced_before_image_decode_or_ocr(
    tmp_path, monkeypatch
):
    _, body, source_store = _job(tmp_path)
    payload = json.loads(body)
    crop = payload["artifacts"]["label_crop"]
    png_header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 20_000, 20_000)
    )
    crop["sha256"] = hashlib.sha256(png_header).hexdigest()
    crop["size_bytes"] = len(png_header)
    identity = (crop["bucket"], crop["key"])
    source_store._objects[identity] = (
        ObjectMetadata(
            bucket=crop["bucket"],
            key=crop["key"],
            sha256=crop["sha256"],
            size_bytes=len(png_header),
            content_type="image/png",
        ),
        png_header,
    )
    store = _ObservedStore(source_store)
    worker, ocr, _ = _worker(store)
    worker.start()
    decode_called = False

    def forbidden_decode(*_args, **_kwargs):
        nonlocal decode_called
        decode_called = True
        raise AssertionError("oversized PNG must be rejected before cv2.imdecode")

    monkeypatch.setattr(
        "label_inspection.worker.inference_worker.cv2.imdecode", forbidden_decode
    )

    with pytest.raises(Exception) as raised:
        worker.process_message(json.dumps(payload).encode("utf-8"))

    assert getattr(raised.value, "code", None) == "IMAGE_TOO_LARGE"
    assert decode_called is False
    assert ocr.recognize_calls == 0


def test_worker_result_persistence_does_not_provision_bucket_per_message(tmp_path):
    _, body, source_store = _job(tmp_path)
    store = _ObservedStore(source_store)
    worker, _, _ = _worker(store)
    worker.start()

    report = worker.process_message(body)

    assert report.durable_result is True
    assert store.ensure_calls == 0


def test_worker_reports_exclusive_download_checksum_and_decode_timings(tmp_path):
    _, body, store = _job(tmp_path)
    worker, _, _ = _worker(store)
    worker.start()

    report = worker.process_message(body)

    assert report.artifact_download_ms >= 0
    assert report.checksum_ms >= 0
    assert report.image_decode_ms >= 0
    assert report.worker_total_ms >= max(
        report.artifact_download_ms,
        report.checksum_ms,
        report.image_decode_ms,
    )
    persisted = report.result.result_payload["stage_timings"]
    assert persisted["image_decode_ms"] == report.image_decode_ms


def test_inference_worker_source_has_no_camera_or_recrop_dependency():
    from pathlib import Path

    source = Path("src/label_inspection/worker/inference_worker.py").read_text("utf-8")

    assert "from ..camera" not in source
    assert "from label_inspection.camera" not in source
    assert "crop_image" not in source
    assert "FixedROI" not in source
