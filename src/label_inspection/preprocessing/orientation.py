"""Explicit camera-frame orientation normalization for the RTSP POC."""

from __future__ import annotations


VALID_ROTATIONS = (0, 90, 180, 270)


def normalize_orientation(frame: object, rotate_degrees: int = 0) -> object:
    """Rotate a camera frame clockwise before FixedROI and OCR."""

    if rotate_degrees not in VALID_ROTATIONS:
        raise ValueError("rotate_degrees must be one of 0, 90, 180, or 270")
    if rotate_degrees == 0:
        return frame

    import cv2

    code = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }[rotate_degrees]
    return cv2.rotate(frame, code)
