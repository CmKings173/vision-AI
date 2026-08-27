import sys
from types import SimpleNamespace

from scripts import debug_yolo_detector


def test_offline_yolo_probe_reports_miss_with_exact_checkpoint(monkeypatch, tmp_path):
    image_path = tmp_path / "detector_input.jpg"
    weights_path = tmp_path / "best.pt"
    output_path = tmp_path / "diagnosis.json"
    image_path.write_bytes(b"image")
    weights_path.write_bytes(b"weights")

    class FakeDetector:
        def __init__(self, path, **kwargs):
            assert path == str(weights_path)
            self.last_debug = {"state": "SUCCESS", "accepted_detection_count": 0}

        def detect(self, image):
            assert image == "decoded-image"
            return []

    monkeypatch.setattr(debug_yolo_detector, "UltralyticsLabelDetector", FakeDetector)
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(imread=lambda path: "decoded-image"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "debug_yolo_detector.py",
            "--image",
            str(image_path),
            "--weights",
            str(weights_path),
            "--output-json",
            str(output_path),
        ],
    )

    assert debug_yolo_detector.main() == 0
    assert '"status": "MISS"' in output_path.read_text(encoding="utf-8")
