"""Conditional perspective correction for candidates with four corners."""

from __future__ import annotations

import math
from typing import Optional, Sequence


def order_quad_points(
    points: Sequence[Sequence[float]],
) -> tuple[tuple[float, float], ...]:
    if len(points) != 4:
        raise ValueError("perspective correction requires exactly four points")
    normalized = [(float(point[0]), float(point[1])) for point in points]
    top_left = min(normalized, key=lambda point: point[0] + point[1])
    bottom_right = max(normalized, key=lambda point: point[0] + point[1])
    top_right = min(normalized, key=lambda point: point[1] - point[0])
    bottom_left = max(normalized, key=lambda point: point[1] - point[0])
    ordered = (top_left, top_right, bottom_right, bottom_left)
    if len(set(ordered)) != 4:
        raise ValueError("perspective points must be distinct")
    return ordered


def rectify_image(
    image: object,
    corners: Optional[Sequence[Sequence[float]]],
    *,
    output_size: Optional[tuple[int, int]] = None,
) -> tuple[object, bool, Optional[str]]:
    """Return ``(image, applied, reason)`` and leave image untouched without corners."""

    if corners is None:
        return image, False, "NO_QUADRILATERAL"
    ordered = order_quad_points(corners)
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("perspective correction requires OpenCV and NumPy") from exc

    shape = getattr(image, "shape", None)
    if shape is None or len(shape) < 2:
        raise ValueError("perspective correction requires an image with shape")
    height, width = int(shape[0]), int(shape[1])
    if output_size is None:
        top_width = math.dist(ordered[0], ordered[1])
        bottom_width = math.dist(ordered[3], ordered[2])
        left_height = math.dist(ordered[0], ordered[3])
        right_height = math.dist(ordered[1], ordered[2])
        target_width = max(1, int(round(max(top_width, bottom_width))))
        target_height = max(1, int(round(max(left_height, right_height))))
    else:
        target_width, target_height = output_size
    source = np.float32(ordered)
    destination = np.float32(
        [(0, 0), (target_width - 1, 0), (target_width - 1, target_height - 1), (0, target_height - 1)]
    )
    matrix = cv2.getPerspectiveTransform(source, destination)
    warped = cv2.warpPerspective(image, matrix, (target_width, target_height))
    return warped, True, None
