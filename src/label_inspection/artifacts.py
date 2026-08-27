"""Persist per-inspection debug artifacts for runtime acceptance evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_result_json(
    root: str | Path,
    event_id: str,
    result_payload: dict[str, Any],
) -> str:
    """Persist a result even when an image encoder is unavailable."""

    event_dir = Path(root).expanduser().resolve() / event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    result_path = event_dir / "result.json"
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(result_path)


def save_inspection_artifacts(
    root: str | Path,
    event_id: str,
    *,
    selected_frame: object,
    label_crop: object | None,
    detector_input: object | None = None,
    detector_debug: dict[str, Any] | None = None,
    result_payload: dict[str, Any],
) -> dict[str, str]:
    """Save event images, detector evidence, and JSON under one event ID.

    ``detector_input`` is intentionally independent from ``label_crop``.  A
    detector miss has no label crop, but its exact input must still be
    inspectable for runtime acceptance diagnosis.
    """

    event_dir = Path(root).expanduser().resolve() / event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    selected_path = event_dir / "selected_frame.jpg"
    result_path = event_dir / "result.json"
    artifacts = {
        "directory": str(event_dir),
        "selected_frame": str(selected_path),
        "result": str(result_path),
    }

    import cv2

    if not cv2.imwrite(str(selected_path), selected_frame):
        raise RuntimeError("SELECTED_FRAME_WRITE_FAILED")
    if label_crop is not None:
        crop_path = event_dir / "label_crop.jpg"
        if not cv2.imwrite(str(crop_path), label_crop):
            raise RuntimeError("LABEL_CROP_WRITE_FAILED")
        artifacts["label_crop"] = str(crop_path)
    if detector_input is not None:
        detector_input_path = event_dir / "detector_input.jpg"
        if not cv2.imwrite(str(detector_input_path), detector_input):
            raise RuntimeError("DETECTOR_INPUT_WRITE_FAILED")
        artifacts["detector_input"] = str(detector_input_path)
    if detector_debug is not None:
        detector_debug_path = event_dir / "detector_debug.json"
        detector_debug_path.write_text(
            json.dumps(detector_debug, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        artifacts["detector_debug"] = str(detector_debug_path)
    result_payload["artifacts"] = artifacts
    write_result_json(root, event_id, result_payload)
    return artifacts
