"""Resident Ultralytics adapter for a future custom ``shipping_label.pt``."""

from __future__ import annotations

from typing import Optional

from ..schemas import LabelCandidate


class UltralyticsLabelDetector:
    """Load YOLO once and normalize its boxes into the V2 domain schema."""

    name = "Ultralytics"
    support_level = "DEFERRED"

    def __init__(
        self,
        model_path: str,
        *,
        device: str = "cpu",
        confidence: float = 0.25,
        iou: float = 0.45,
        image_size: int = 640,
        max_det: int = 10,
        class_name: Optional[str] = "shipping_label",
    ) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "UltralyticsLabelDetector requires the optional detector dependencies"
            ) from exc
        self.model = YOLO(model_path)
        self.device = device
        self.confidence = confidence
        self.iou = iou
        self.image_size = image_size
        self.max_det = max_det
        self.class_name = class_name

    def detect(self, frame: object, *, frame_id: int | None = None) -> list[LabelCandidate]:
        results = self.model.predict(
            source=frame,
            imgsz=self.image_size,
            conf=self.confidence,
            iou=self.iou,
            max_det=self.max_det,
            device=self.device,
            verbose=False,
        )
        candidates: list[LabelCandidate] = []
        for result in results or []:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            xyxy = _tolist(getattr(boxes, "xyxy", []))
            scores = _flat_list(getattr(boxes, "conf", []))
            classes = _flat_list(getattr(boxes, "cls", []))
            names = getattr(result, "names", {}) or {}
            for index, box in enumerate(xyxy):
                if len(box) < 4:
                    continue
                class_id = int(classes[index]) if index < len(classes) else None
                label_name = names.get(class_id, str(class_id)) if isinstance(names, dict) else None
                if self.class_name and label_name not in {self.class_name, None}:
                    continue
                score = float(scores[index]) if index < len(scores) else self.confidence
                candidates.append(
                    LabelCandidate(
                        bbox=tuple(float(value) for value in box[:4]),  # type: ignore[arg-type]
                        confidence=score,
                        detector=self.name,
                        frame_id=frame_id,
                    )
                )
        return candidates


def _tolist(value: object) -> list[list[float]]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        if not value:
            return []
        if isinstance(value[0], (tuple, list)):
            return [list(map(float, row)) for row in value]
        return [[float(item)] for item in value]
    return []


def _flat_list(value: object) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        flattened: list[float] = []
        for item in value:
            if isinstance(item, (tuple, list)):
                flattened.extend(float(part) for part in item[:1])
            else:
                flattened.append(float(item))
        return flattened
    return []
