"""Bounding-box padding, clamping, and crop provenance."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..detection.fixed_roi import clamp_bbox, frame_size


@dataclass(frozen=True)
class CropResult:
    image: object
    bbox: tuple[float, float, float, float]
    source_bbox: tuple[float, float, float, float]
    truncated: bool = False


def pad_bbox(
    bbox: tuple[float, float, float, float],
    *,
    width: int,
    height: int,
    padding_ratio: float = 0.05,
) -> tuple[float, float, float, float]:
    if padding_ratio < 0:
        raise ValueError("padding_ratio must be >= 0")
    x1, y1, x2, y2 = bbox
    pad_x = (x2 - x1) * padding_ratio
    pad_y = (y2 - y1) * padding_ratio
    return clamp_bbox((x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y), width, height)


def crop_image(
    frame: object,
    bbox: tuple[float, float, float, float],
    *,
    padding_ratio: float = 0.0,
) -> CropResult:
    width, height = frame_size(frame)
    source_bbox = bbox
    x1, y1, x2, y2 = bbox
    pad_x = (x2 - x1) * padding_ratio
    pad_y = (y2 - y1) * padding_ratio
    padded_bbox = (x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y)
    bounded = pad_bbox(
        bbox,
        width=width,
        height=height,
        padding_ratio=padding_ratio,
    )
    x1, y1, x2, y2 = bounded
    ix1, iy1, ix2, iy2 = math.floor(x1), math.floor(y1), math.ceil(x2), math.ceil(y2)
    if ix2 <= ix1 or iy2 <= iy1:
        raise ValueError("bbox produces an empty crop")

    try:
        cropped = frame[iy1:iy2, ix1:ix2]  # type: ignore[index]
    except (TypeError, IndexError):
        rows = frame[iy1:iy2]  # type: ignore[index]
        cropped = [row[ix1:ix2] for row in rows]
    truncated = bounded != padded_bbox
    return CropResult(
        image=cropped,
        bbox=(float(ix1), float(iy1), float(ix2), float(iy2)),
        source_bbox=source_bbox,
        truncated=truncated,
    )
