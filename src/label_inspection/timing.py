"""Canonical high-resolution timing helpers for inspection stages."""

from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Iterator


TIMING_KEYS = (
    "frame_selection_ms",
    "detection_ms",
    "crop_rectify_ms",
    "quality_ms",
    "candidate_ranking_ms",
    "ocr_ms",
    "barcode_ms",
    "field_extraction_ms",
    "validation_ms",
    "total_ms",
)


def new_timing() -> dict[str, float]:
    return {key: 0.0 for key in TIMING_KEYS}


@contextmanager
def timed(stage_timings: dict[str, float], name: str) -> Iterator[None]:
    start = perf_counter()
    try:
        yield
    finally:
        stage_timings[name] = stage_timings.get(name, 0.0) + (perf_counter() - start) * 1000.0
