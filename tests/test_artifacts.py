import json
import sys
import types

from label_inspection.artifacts import save_inspection_artifacts


def test_inspection_artifacts_are_grouped_by_event_id(tmp_path, monkeypatch):
    writes = []

    def fake_imwrite(path, image):
        writes.append((path, image))
        return True

    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(imwrite=fake_imwrite))
    payload = {"status": "REVIEW", "event_id": "INS-ABC"}

    paths = save_inspection_artifacts(
        tmp_path,
        "INS-ABC",
        selected_frame="frame",
        label_crop="crop",
        result_payload=payload,
    )

    assert paths["selected_frame"].endswith("INS-ABC\\selected_frame.jpg") or paths[
        "selected_frame"
    ].endswith("INS-ABC/selected_frame.jpg")
    assert [image for _, image in writes] == ["frame", "crop"]
    saved = json.loads((tmp_path / "INS-ABC" / "result.json").read_text(encoding="utf-8"))
    assert saved["artifacts"]["result"].endswith("result.json")


def test_inspection_artifacts_save_detector_input_and_debug_for_misses(
    tmp_path, monkeypatch
):
    writes = []

    def fake_imwrite(path, image):
        writes.append((path, image))
        return True

    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(imwrite=fake_imwrite))
    payload = {"status": "REVIEW", "event_id": "INS-MISS"}

    paths = save_inspection_artifacts(
        tmp_path,
        "INS-MISS",
        selected_frame="selected",
        label_crop=None,
        detector_input="detector-input",
        detector_debug={"state": "SUCCESS", "accepted_detection_count": 0},
        result_payload=payload,
    )

    assert paths["detector_input"].endswith("detector_input.jpg")
    assert paths["detector_debug"].endswith("detector_debug.json")
    assert [image for _, image in writes] == ["selected", "detector-input"]
    debug = json.loads(
        (tmp_path / "INS-MISS" / "detector_debug.json").read_text(encoding="utf-8")
    )
    assert debug["accepted_detection_count"] == 0
    saved = json.loads((tmp_path / "INS-MISS" / "result.json").read_text(encoding="utf-8"))
    assert saved["artifacts"]["detector_input"].endswith("detector_input.jpg")
