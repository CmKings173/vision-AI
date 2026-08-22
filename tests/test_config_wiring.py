from dataclasses import replace

import pytest

from label_inspection.app import build_pipeline
from label_inspection.camera.security import resolve_camera_source
from label_inspection.config import Settings
from label_inspection.ocr.tensorrt_ocr import TensorRTOCRAdapter
from label_inspection.ocr.ppocr_v6 import PPOCRV6TransformersAdapter


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
    assert set(pipeline.extractor.fields) == {
        "sku",
        "lot",
        "tracking_number",
        "order_id",
    }


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
