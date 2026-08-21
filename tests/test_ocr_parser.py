from types import SimpleNamespace

import numpy as np

from label_inspection.ocr.ppocr import PPOCRAdapter, normalize_paddle_result


def test_normalize_current_paddle_result_shape():
    lines = normalize_paddle_result(
        [
            {
                "rec_texts": ["SKU: ABC123", "LOT: L42"],
                "rec_scores": [0.91, 0.82],
                "rec_polys": [[[0, 0], [1, 0], [1, 1], [0, 1]], [[2, 2], [3, 2], [3, 3], [2, 3]]],
            }
        ]
    )
    assert [(line.text, line.confidence) for line in lines] == [
        ("SKU: ABC123", 0.91),
        ("LOT: L42", 0.82),
    ]


def test_normalize_numpy_polygons_without_ambiguous_truth_value():
    lines = normalize_paddle_result(
        [
            {
                "rec_texts": ["SKU: ABC123"],
                "rec_scores": np.array([0.91]),
                "rec_polys": np.array([[[0, 0], [10, 0], [10, 5], [0, 5]]]),
            }
        ]
    )
    assert lines[0].polygon == [[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]]


def test_normalize_legacy_paddle_result_shape():
    lines = normalize_paddle_result(
        [[[[0, 0], [1, 0], [1, 1], [0, 1]], ("SKU: ABC123", 0.9)]]
    )
    assert lines[0].text == "SKU: ABC123"
    assert lines[0].confidence == 0.9


def test_ppocr_loads_once_and_returns_structured_lines(monkeypatch):
    class FakeOCR:
        instances = 0

        def __init__(self, **kwargs):
            type(self).instances += 1

        def predict(self, image):
            return [{"rec_texts": ["SKU: ABC123"], "rec_scores": [0.95]}]

    monkeypatch.setitem(__import__("sys").modules, "paddleocr", SimpleNamespace(PaddleOCR=FakeOCR))
    adapter = PPOCRAdapter()
    first = adapter.recognize(object())
    second = adapter.recognize(object())

    assert FakeOCR.instances == 1
    assert first.lines[0].text == second.lines[0].text == "SKU: ABC123"
