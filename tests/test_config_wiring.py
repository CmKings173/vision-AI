import sys
import types
from dataclasses import replace
from typing import ClassVar

import pytest

from label_inspection.app import build_local_spool, build_pipeline
from label_inspection.camera.security import resolve_camera_source
from label_inspection.config import Settings
from label_inspection.ocr.ppocr_v6 import PPOCRV6TransformersAdapter
from label_inspection.ocr.tensorrt_ocr import TensorRTOCRAdapter


def valid_settings(**overrides):
    base = Settings(
        detector="fixed-roi",
        label_roi="0.1,0.1,0.9,0.9",
        roi_normalized=True,
        ocr_engine="ppocr",
        barcode_engine="zxing",
        detector_device="cpu",
        ocr_device="cpu",
    )
    return replace(base, **overrides)


def test_fixed_roi_mode_requires_explicit_roi_at_runtime():
    with pytest.raises(ValueError, match="requires VISION_LABEL_ROI"):
        valid_settings(label_roi=None).validate()


def test_unknown_ocr_or_barcode_engine_fails_instead_of_falling_back():
    with pytest.raises(ValueError, match="VISION_OCR_ENGINE"):
        valid_settings(ocr_engine="glm").validate()
    with pytest.raises(ValueError, match="VISION_BARCODE_ENGINE"):
        valid_settings(barcode_engine="opencv").validate()


def test_separate_ocr_device_and_confidence_are_wired():
    pipeline = build_pipeline(
        valid_settings(
            detector_device="cuda:0",
            ocr_device="gpu:0",
            ocr_confidence=0.83,
        )
    )

    assert pipeline.ocr.device == "gpu:0"
    assert pipeline.validator.min_field_confidence == 0.83


def test_default_pipeline_is_profile_free_and_cannot_semantically_pass():
    pipeline = build_pipeline(valid_settings())

    assert pipeline.extractor.fields == ()
    assert pipeline.extractor.profile_name is None
    assert pipeline.validator.required_fields == ()
    assert pipeline.validator.profile_name is None
    assert pipeline.validator.profile_approved is False


def test_dgx_profile_keeps_semantic_blocker_and_cannot_authorize_pass():
    pipeline = build_pipeline(
        valid_settings(
            extraction_profile="dgx_spark_label",
            required_fields=("customer_part_number",),
        )
    )

    assert pipeline.extractor.profile_name == "dgx_spark_label"
    assert pipeline.extractor.semantic_blockers
    assert pipeline.validator.profile_approved is False


def test_trained_yolo_detector_is_wired_with_normalized_gpu_device(monkeypatch, tmp_path):
    class FakeYOLO:
        names: ClassVar = {0: "shipping_label"}

        def __init__(self, path):
            self.path = path

    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeYOLO))
    model_path = str(tmp_path / "shipping_label_best.pt")
    (tmp_path / "shipping_label_best.pt").write_bytes(b"weights")

    pipeline = build_pipeline(
        valid_settings(
            detector="yolo",
            detector_model=model_path,
            detector_device="cpu",
        )
    )

    assert pipeline.detector.name == "Ultralytics"
    assert pipeline.detector.device == "cpu"
    assert pipeline.detector.model.path == model_path


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("detector_confidence", 1.1, "VISION_DETECTOR_CONFIDENCE"),
        ("detector_iou", -0.1, "VISION_DETECTOR_IOU"),
        ("detector_image_size", 0, "VISION_DETECTOR_IMGSZ"),
        ("detector_max_det", 0, "VISION_DETECTOR_MAX_DET"),
    ],
)
def test_invalid_yolo_runtime_configuration_fails_validation(field, value, message):
    with pytest.raises(ValueError, match=message):
        valid_settings(**{field: value}).validate_station()


def test_yolo_runtime_configuration_is_wired(monkeypatch, tmp_path):
    class FakeYOLO:
        names: ClassVar = {0: "shipping_label"}

        def __init__(self, path):
            self.path = path

    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeYOLO))
    weights = tmp_path / "shipping_label.pt"
    weights.write_bytes(b"weights")
    pipeline = build_pipeline(
        valid_settings(
            detector="yolo",
            detector_model=str(weights),
            detector_device="cpu",
            detector_confidence=0.31,
            detector_iou=0.42,
            detector_image_size=768,
            detector_max_det=7,
        )
    )

    assert pipeline.detector.confidence == 0.31
    assert pipeline.detector.iou == 0.42
    assert pipeline.detector.image_size == 768
    assert pipeline.detector.max_det == 7


def test_tensorrt_ocr_engine_is_wired_without_loading_gpu_runtime():
    pipeline = build_pipeline(
        valid_settings(
            ocr_engine="tensorrt",
            ocr_device="cuda:0",
            ocr_det_engine="models/ppocr/det.engine",
            ocr_rec_engine="models/ppocr/rec.engine",
            ocr_char_dict="models/ppocr/ppocr_keys_v1.txt",
        )
    )

    assert isinstance(pipeline.ocr, TensorRTOCRAdapter)
    assert pipeline.ocr.engine == "tensorrt-ppocr"


def test_ppocr_v6_transformers_engine_is_wired_without_loading_runtime():
    pipeline = build_pipeline(
        valid_settings(
            ocr_engine="ppocr_v6",
            ocr_backend="transformers",
            ocr_version="PP-OCRv6",
            ocr_device="gpu:0",
            required_fields=("tracking_number", "order_id"),
        )
    )

    assert isinstance(pipeline.ocr, PPOCRV6TransformersAdapter)
    assert pipeline.ocr.engine == "ppocr_v6"
    assert pipeline.ocr.backend == "transformers"
    assert pipeline.extractor.fields == ()
    assert pipeline.validator.required_fields == ()


def test_dgx_spark_extraction_profile_wires_only_label_fields():
    pipeline = build_pipeline(
        valid_settings(
            extraction_profile="dgx_spark_label",
            required_fields=(
                "customer_part_number",
                "so_number",
                "our_part_number",
                "quantity",
                "net_weight",
                "gross_weight",
                "carton_number",
            ),
        )
    )

    assert pipeline.extractor.fields == (
        "customer_part_number",
        "so_number",
        "our_part_number",
        "quantity",
        "net_weight",
        "gross_weight",
        "carton_number",
    )


def test_tensorrt_ocr_requires_engine_and_dictionary_paths():
    with pytest.raises(ValueError, match="VISION_OCR_DET_ENGINE"):
        valid_settings(ocr_engine="tensorrt").validate()


def test_malformed_roi_is_rejected_when_pipeline_is_built():
    with pytest.raises(ValueError, match="x2 > x1"):
        build_pipeline(valid_settings(label_roi="0.8,0.1,0.2,0.9"))


def test_rtsp_env_is_default_and_cli_source_has_explicit_precedence(monkeypatch):
    monkeypatch.setenv("VISION_RTSP_URL", "rtsp://env-host/stream")
    configured = Settings().rtsp_url

    assert resolve_camera_source(None, configured) == "rtsp://env-host/stream"
    assert (
        resolve_camera_source("rtsp://cli-host/stream", configured)
        == "rtsp://cli-host/stream"
    )
    assert resolve_camera_source("http://phone-host:8080/video", None) == (
        "http://phone-host:8080/video"
    )


def test_missing_rtsp_source_fails_clearly(monkeypatch):
    monkeypatch.delenv("VISION_RTSP_URL", raising=False)

    with pytest.raises(ValueError, match="VISION_RTSP_URL"):
        resolve_camera_source(None, Settings().rtsp_url)


def test_frame_preview_long_edge_is_bounded_for_preselection():
    valid_settings(frame_preview_long_edge=320).validate()
    valid_settings(frame_preview_long_edge=640).validate()
    with pytest.raises(ValueError, match="PREVIEW_LONG_EDGE"):
        valid_settings(frame_preview_long_edge=1280).validate()


def test_camera_rotation_is_limited_to_quarter_turns():
    valid_settings(camera_rotate_degrees=90).validate()
    with pytest.raises(ValueError, match="VISION_CAMERA_ROTATE_DEG"):
        valid_settings(camera_rotate_degrees=45).validate()


def test_station_spool_limits_are_environment_configurable(monkeypatch):
    monkeypatch.setenv("VISION_SPOOL_ROOT", "/var/lib/vision/spool")
    monkeypatch.setenv("VISION_SPOOL_MAX_PENDING_EVENTS", "42")
    monkeypatch.setenv("VISION_SPOOL_MAX_PENDING_BYTES", "123456")
    monkeypatch.setenv("VISION_SPOOL_MIN_FREE_DISK_BYTES", "654321")

    configured = Settings()

    assert configured.spool_root == "/var/lib/vision/spool"
    assert configured.spool_max_pending_events == 42
    assert configured.spool_max_pending_bytes == 123456
    assert configured.spool_min_free_disk_bytes == 654321


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"spool_root": "  "}, "VISION_SPOOL_ROOT"),
        ({"spool_max_pending_events": 0}, "MAX_PENDING_EVENTS"),
        ({"spool_max_pending_bytes": 0}, "MAX_PENDING_BYTES"),
        ({"spool_min_free_disk_bytes": -1}, "MIN_FREE_DISK_BYTES"),
    ],
)
def test_invalid_station_spool_configuration_fails_validation(override, message):
    with pytest.raises(ValueError, match=message):
        valid_settings(**override).validate_station()


def test_local_spool_factory_wires_station_capacity_settings(tmp_path):
    spool = build_local_spool(
        valid_settings(
            spool_root=str(tmp_path / "spool"),
            spool_max_pending_events=12,
            spool_max_pending_bytes=3456,
            spool_min_free_disk_bytes=789,
        )
    )

    assert spool.root == (tmp_path / "spool").resolve()
    assert spool.limits.max_pending_events == 12
    assert spool.limits.max_pending_bytes == 3456
    assert spool.limits.min_free_disk_bytes == 789
