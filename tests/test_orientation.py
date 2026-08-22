import sys
import types

import pytest

from label_inspection.preprocessing.orientation import normalize_orientation


def test_orientation_zero_keeps_frame_identity():
    frame = object()
    assert normalize_orientation(frame, 0) is frame


def test_orientation_90_uses_clockwise_cv2_rotation(monkeypatch):
    calls = []
    fake_cv2 = types.SimpleNamespace(
        ROTATE_90_CLOCKWISE=1,
        ROTATE_180=2,
        ROTATE_90_COUNTERCLOCKWISE=3,
        rotate=lambda frame, code: calls.append((frame, code)) or "rotated",
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    assert normalize_orientation("frame", 90) == "rotated"
    assert calls == [("frame", 1)]


def test_orientation_rejects_unsupported_degrees():
    with pytest.raises(ValueError, match="one of 0, 90, 180, or 270"):
        normalize_orientation(object(), 45)
