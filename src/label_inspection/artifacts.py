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
    label_crop: object,
    result_payload: dict[str, Any],
) -> dict[str, str]:
    """Save the selected frame, processed crop, and JSON under one event ID."""

    import cv2

    event_dir = Path(root).expanduser().resolve() / event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    selected_path = event_dir / "selected_frame.jpg"
    crop_path = event_dir / "label_crop.jpg"
    result_path = event_dir / "result.json"
    if not cv2.imwrite(str(selected_path), selected_frame):
        raise RuntimeError("SELECTED_FRAME_WRITE_FAILED")
    if not cv2.imwrite(str(crop_path), label_crop):
        raise RuntimeError("LABEL_CROP_WRITE_FAILED")
    artifacts = {
        "directory": str(event_dir),
        "selected_frame": str(selected_path),
        "label_crop": str(crop_path),
        "result": str(result_path),
    }
    result_payload["artifacts"] = artifacts
    write_result_json(root, event_id, result_payload)
    return artifacts
