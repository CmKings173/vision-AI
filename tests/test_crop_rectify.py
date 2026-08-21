from types import SimpleNamespace

import numpy as np
import pytest

from label_inspection.preprocessing.crop import crop_image, pad_bbox
from label_inspection.preprocessing.rectify import order_quad_points, rectify_image


class Matrix:
    shape = (10, 20, 3)

    def __getitem__(self, key):
        assert isinstance(key, tuple)
        y_slice, x_slice = key
        return SimpleNamespace(shape=(y_slice.stop - y_slice.start, x_slice.stop - x_slice.start, 3))


def test_padding_is_clamped_to_image_edges_and_crop_has_provenance():
    bbox = pad_bbox((0, 2, 18, 9), width=20, height=10, padding_ratio=0.5)
    assert bbox == (0.0, 0.0, 20.0, 10.0)

    crop = crop_image(Matrix(), (1, 2, 10, 8), padding_ratio=0.1)
    assert crop.bbox == (0.0, 1.0, 11.0, 9.0)
    assert crop.source_bbox == (1, 2, 10, 8)
    assert not crop.truncated


def test_quad_order_and_no_quad_fallback():
    assert order_quad_points([(90, 80), (10, 10), (90, 10), (10, 80)]) == (
        (10.0, 10.0),
        (90.0, 10.0),
        (90.0, 80.0),
        (10.0, 80.0),
    )
    image = object()
    assert rectify_image(image, None) == (image, False, "NO_QUADRILATERAL")
    with pytest.raises(ValueError):
        order_quad_points([(0, 0), (1, 1), (2, 2)])


def test_valid_quadrilateral_runs_actual_opencv_warp():
    image = np.zeros((100, 160, 3), dtype=np.uint8)
    warped, applied, reason = rectify_image(
        image,
        [(10, 10), (150, 5), (145, 90), (15, 95)],
    )
    assert applied is True
    assert reason is None
    assert warped.shape[0] > 0
    assert warped.shape[1] > 0
