import json
import sys
import types

from label_inspection.barcode.base import NullBarcodeDecoder
from label_inspection.barcode.zxing import ZXingBarcodeDecoder
from label_inspection.schemas import InspectionResult


def test_missing_zxing_dependency_is_structured_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "zxingcpp", None)
    result = ZXingBarcodeDecoder(use_variants=False).decode(object())[0]
    assert result.value is None
    assert result.error == "ZXING_NOT_INSTALLED"


def test_zxing_result_is_normalized_and_deduplicated(monkeypatch):
    class Format:
        name = "QR_CODE"

    item = types.SimpleNamespace(text="ABC", format=Format(), valid=True, position=None)
    fake = types.SimpleNamespace(read_barcodes=lambda image: [item, item])
    monkeypatch.setitem(sys.modules, "zxingcpp", fake)

    results = ZXingBarcodeDecoder(use_variants=False).decode(object())

    assert len(results) == 1
    assert results[0].value == "ABC"
    assert results[0].format == "QR_CODE"
    assert results[0].valid is True
    assert NullBarcodeDecoder().decode(object()) == []


def test_zxing_real_shape_position_is_json_safe_and_uses_valid_field(monkeypatch):
    class Point:
        __slots__ = ("x", "y")

        def __init__(self, x, y):
            self.x = x
            self.y = y

    class Position:
        __slots__ = ("top_left", "top_right", "bottom_right", "bottom_left")

        def __init__(self):
            self.top_left = Point(1, 2)
            self.top_right = Point(30, 2)
            self.bottom_right = Point(30, 40)
            self.bottom_left = Point(1, 40)

    item = types.SimpleNamespace(
        text="ABC123",
        format="Code128",
        valid=False,
        is_valid=True,  # proves the deprecated/wrong field is ignored
        position=Position(),
    )
    monkeypatch.setitem(
        sys.modules,
        "zxingcpp",
        types.SimpleNamespace(read_barcodes=lambda image: [item]),
    )

    barcode = ZXingBarcodeDecoder(use_variants=False).decode(object())[0]
    payload = InspectionResult(event_id="INS-ZX", camera_id="CAM", barcode=barcode).to_dict()

    assert barcode.valid is False
    assert payload["barcode"]["status"] == "SUCCESS"
    assert barcode.position == {
        "top_left": [1.0, 2.0],
        "top_right": [30.0, 2.0],
        "bottom_right": [30.0, 40.0],
        "bottom_left": [1.0, 40.0],
    }
    json.dumps(payload)


def test_zxing_prepare_loads_runtime_before_first_decode(monkeypatch):
    fake = types.SimpleNamespace(read_barcodes=lambda image: [])
    monkeypatch.setitem(__import__("sys").modules, "zxingcpp", fake)
    decoder = ZXingBarcodeDecoder(use_variants=False)

    assert decoder.ready is False
    assert decoder.prepare() is True
    assert decoder.ready is True
    assert decoder.decode(object())[0].value is None
