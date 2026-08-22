import json
from types import SimpleNamespace

from label_inspection.ocr.ppocr_v6 import PPOCRV6TransformersAdapter


def test_ppocr_v6_transformers_uses_required_constructor_and_loads_once(monkeypatch):
    calls = []

    class FakeOCR:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def predict(self, image):
            return [{
                "rec_texts": ["SPXVN067769969098"],
                "rec_scores": [0.97],
                "rec_polys": [[[1, 2], [20, 2], [20, 8], [1, 8]]],
            }]

    monkeypatch.setitem(
        __import__("sys").modules,
        "paddleocr",
        SimpleNamespace(PaddleOCR=FakeOCR),
    )
    adapter = PPOCRV6TransformersAdapter(device="gpu:0")

    first = adapter.recognize(object())
    second = adapter.recognize(object())

    assert len(calls) == 1
    assert calls[0] == {
        "ocr_version": "PP-OCRv6",
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "engine": "transformers",
        "device": "gpu:0",
    }
    assert first.success and second.success
    assert first.engine == second.engine == "ppocr_v6"
    assert first.backend == second.backend == "transformers"
    assert first.device == second.device == "gpu:0"
    assert first.lines[0].text == "SPXVN067769969098"
    json.dumps(first.to_dict())


def test_ppocr_v6_load_failure_is_structured(monkeypatch):
    def fail_import(name):
        raise ImportError("missing paddleocr")

    monkeypatch.setattr("importlib.import_module", fail_import)
    adapter = PPOCRV6TransformersAdapter()

    result = adapter.recognize(object())

    assert not result.success
    assert result.error_code == "PP-OCRV6_DEPENDENCY_MISSING"
    assert result.backend == "transformers"
