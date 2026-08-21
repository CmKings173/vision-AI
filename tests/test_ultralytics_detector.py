import sys
import types

from label_inspection.detection.ultralytics_detector import UltralyticsLabelDetector


class FakeBoxes:
    xyxy = [[1, 2, 30, 40]]
    conf = [0.88]
    cls = [0]


class FakeModel:
    predict_calls = 0

    def __init__(self, path):
        assert path == "shipping_label.pt"

    def predict(self, **kwargs):
        type(self).predict_calls += 1
        assert kwargs["device"] == "cpu"
        return [types.SimpleNamespace(boxes=FakeBoxes(), names={0: "shipping_label"})]


def test_ultralytics_model_loads_once_and_normalizes_box(monkeypatch):
    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeModel))

    detector = UltralyticsLabelDetector("shipping_label.pt")
    candidates = detector.detect(types.SimpleNamespace(shape=(50, 60, 3)), frame_id=2)

    assert candidates[0].bbox == (1.0, 2.0, 30.0, 40.0)
    assert candidates[0].confidence == 0.88
    assert FakeModel.predict_calls == 1
