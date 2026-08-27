import hashlib
import sys
import types
from typing import ClassVar

import pytest

from label_inspection.detection.ultralytics_detector import UltralyticsLabelDetector


class FakeBoxes:
    xyxy: ClassVar = [[1, 2, 30, 40]]
    conf: ClassVar = [0.88]
    cls: ClassVar = [0]


class FakeModel:
    predict_calls: ClassVar = 0
    last_predict_kwargs: ClassVar = None
    names: ClassVar = {0: "shipping_label"}

    def __init__(self, path):
        self.path = path

    def predict(self, **kwargs):
        type(self).predict_calls += 1
        type(self).last_predict_kwargs = kwargs
        assert kwargs["device"] == "cpu"
        return [types.SimpleNamespace(boxes=FakeBoxes(), names={0: "shipping_label"})]


def test_ultralytics_model_loads_once_and_normalizes_box(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeModel))
    weights = tmp_path / "shipping_label.pt"
    weights.write_bytes(b"weights")

    detector = UltralyticsLabelDetector(str(weights))
    candidates = detector.detect(types.SimpleNamespace(shape=(50, 60, 3)), frame_id=2)

    assert candidates[0].bbox == (1.0, 2.0, 30.0, 40.0)
    assert candidates[0].confidence == 0.88
    assert FakeModel.predict_calls == 1


def test_ultralytics_normalizes_gpu_device_alias_for_inference(monkeypatch, tmp_path):
    class GPUModel(FakeModel):
        def predict(self, **kwargs):
            type(self).last_predict_kwargs = kwargs
            return [types.SimpleNamespace(boxes=FakeBoxes(), names={0: "shipping_label"})]

    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=GPUModel))
    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(
            __version__="test",
            version=types.SimpleNamespace(cuda="13.0"),
            cuda=types.SimpleNamespace(
                is_available=lambda: True,
                device_count=lambda: 1,
                get_device_name=lambda index: "Fake GPU",
            ),
        ),
    )
    weights = tmp_path / "shipping_label.pt"
    weights.write_bytes(b"weights")

    detector = UltralyticsLabelDetector(str(weights), device="gpu:0")
    detector.detect(types.SimpleNamespace(shape=(50, 60, 3)))

    assert GPUModel.last_predict_kwargs["device"] == "cuda:0"


def test_ultralytics_warmup_marks_detector_ready(monkeypatch, tmp_path):
    class WarmupModel:
        names: ClassVar = {0: "shipping_label"}

        def __init__(self, path):
            self.path = path
            self.calls = []

        def predict(self, **kwargs):
            self.calls.append(kwargs)
            return []

    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=WarmupModel))
    weights = tmp_path / "shipping_label.pt"
    weights.write_bytes(b"weights")

    detector = UltralyticsLabelDetector(str(weights), device="cpu")

    assert detector.ready is False
    elapsed = detector.warmup()

    assert elapsed >= 0
    assert detector.ready is True
    assert detector.model.calls[0]["device"] == "cpu"


def test_ultralytics_records_checkpoint_identity_and_detection_debug(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeModel))
    weights = tmp_path / "shipping_label.pt"
    weights.write_bytes(b"weights")

    detector = UltralyticsLabelDetector(
        str(weights), confidence=0.31, iou=0.42, image_size=768, max_det=7
    )
    detector.detect(types.SimpleNamespace(shape=(50, 60, 3)), frame_id=9)

    expected_sha = hashlib.sha256(b"weights").hexdigest()
    metadata = detector.runtime_metadata
    debug = detector.last_debug
    assert metadata["model_path"] == str(weights.resolve())
    assert metadata["model_sha256"] == expected_sha
    assert metadata["class_mapping"] == {"0": "shipping_label"}
    assert metadata["configured_device"] == "cpu"
    assert metadata["actual_device"] == "cpu"
    assert metadata["confidence"] == 0.31
    assert metadata["iou"] == 0.42
    assert metadata["imgsz"] == 768
    assert metadata["max_det"] == 7
    assert debug["event_frame_id"] == 9
    assert debug["raw_detection_count"] == 1
    assert debug["accepted_detection_count"] == 1
    assert debug["raw_detections"][0]["class_name"] == "shipping_label"
    assert debug["accepted_detections"][0]["accepted"] is True
    assert debug["inference_ms"] >= 0


def test_ultralytics_fails_closed_when_checkpoint_schema_lacks_shipping_label(
    monkeypatch, tmp_path
):
    class WrongSchemaModel:
        names: ClassVar = {0: "person"}

        def __init__(self, path):
            self.path = path

    monkeypatch.setitem(
        sys.modules, "ultralytics", types.SimpleNamespace(YOLO=WrongSchemaModel)
    )
    weights = tmp_path / "wrong.pt"
    weights.write_bytes(b"weights")

    with pytest.raises(ValueError, match="shipping_label"):
        UltralyticsLabelDetector(str(weights))


def test_ultralytics_fails_closed_when_cuda_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeModel))
    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            __version__="test",
        ),
    )
    weights = tmp_path / "shipping_label.pt"
    weights.write_bytes(b"weights")

    with pytest.raises(RuntimeError, match="CUDA_UNAVAILABLE"):
        UltralyticsLabelDetector(str(weights), device="gpu:0")
