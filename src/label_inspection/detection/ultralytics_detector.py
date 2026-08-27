"""Resident Ultralytics adapter for the experimental custom label detector.

The adapter deliberately treats a trained checkpoint as a runtime contract:
the file, class schema, configured thresholds, and actual execution device are
recorded before the detector can become ready.  This prevents a generic YOLO
checkpoint or a CPU fallback from being mistaken for the GX10 detector.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import time
from pathlib import Path
from typing import Any

from ..schemas import LabelCandidate


class UltralyticsLabelDetector:
    """Load YOLO once and normalize its boxes into the V2 domain schema."""

    name = "Ultralytics"
    # A checkpoint can be technically runnable while still failing live
    # runtime acceptance.  Promotion to SUPPORTED requires measured evidence.
    support_level = "EXPERIMENTAL"

    def __init__(
        self,
        model_path: str,
        *,
        device: str = "cpu",
        confidence: float = 0.25,
        iou: float = 0.45,
        image_size: int = 640,
        max_det: int = 10,
        class_name: str | None = "shipping_label",
    ) -> None:
        self.model_path = str(Path(model_path).expanduser().resolve())
        model_file = Path(self.model_path)
        if not model_file.is_file():
            raise FileNotFoundError(f"detector model not found: {self.model_path}")
        self.model_sha256 = _sha256_file(model_file)
        self.device = _normalize_device(device)
        self.actual_device = _verify_actual_device(self.device)
        _validate_detector_options(confidence, iou, image_size, max_det)
        self.confidence = float(confidence)
        self.iou = float(iou)
        self.image_size = int(image_size)
        self.max_det = int(max_det)
        self.class_name = class_name or "shipping_label"

        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "UltralyticsLabelDetector requires the optional detector dependencies"
            ) from exc
        self.model = YOLO(self.model_path)
        self.class_mapping = _class_mapping(
            getattr(self.model, "names", None)
            or getattr(getattr(self.model, "model", None), "names", None)
        )
        if self.class_name not in self.class_mapping.values():
            raise ValueError(
                "detector checkpoint class schema must contain "
                f"{self.class_name!r}; got {self.class_mapping}"
            )
        torch_version, cuda_version = _torch_versions()
        self._runtime_versions = {
            "ultralytics_version": _package_version("ultralytics"),
            "torch_version": torch_version,
            "cuda_version": cuda_version,
            "actual_cuda_device_name": _cuda_device_name(self.actual_device),
        }
        self.ready = False
        self.last_debug: dict[str, Any] = {
            **self.runtime_metadata,
            "event_frame_id": None,
            "inference_ms": None,
            "raw_detection_count": 0,
            "accepted_detection_count": 0,
            "raw_detections": [],
            "accepted_detections": [],
            "state": "NOT_RUN",
        }

    @property
    def runtime_metadata(self) -> dict[str, Any]:
        """Return immutable-run metadata suitable for telemetry/provenance."""

        return {
            "model_path": self.model_path,
            "model_name": Path(self.model_path).stem,
            "model_version": "unknown",
            "model_sha256": self.model_sha256,
            "configured_device": self.device,
            "actual_device": self.actual_device,
            **self._runtime_versions,
            "confidence": self.confidence,
            "iou": self.iou,
            "imgsz": self.image_size,
            "max_det": self.max_det,
            "class_mapping": dict(self.class_mapping),
            "expected_class": self.class_name,
        }

    def warmup(self) -> float:
        """Run one resident-model inference and return elapsed milliseconds."""

        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - required project dependency
            raise RuntimeError("UltralyticsLabelDetector warmup requires NumPy") from exc
        started = time.perf_counter()
        try:
            self.model.predict(
                **self._predict_kwargs(
                    source=np.zeros(
                        (self.image_size, self.image_size, 3), dtype=np.uint8
                    )
                )
            )
        except Exception:
            self.ready = False
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.ready = True
        self.last_debug = {
            **self.runtime_metadata,
            "event_frame_id": None,
            "inference_ms": elapsed_ms,
            "raw_detection_count": 0,
            "accepted_detection_count": 0,
            "raw_detections": [],
            "accepted_detections": [],
            "state": "WARMED",
        }
        return elapsed_ms

    def detect(self, frame: object, *, frame_id: int | None = None) -> list[LabelCandidate]:
        started = time.perf_counter()
        raw_detections: list[dict[str, Any]] = []
        accepted_detections: list[dict[str, Any]] = []
        try:
            results = self.model.predict(**self._predict_kwargs(source=frame))
            candidates: list[LabelCandidate] = []
            for result in results or []:
                boxes = getattr(result, "boxes", None)
                if boxes is None:
                    continue
                xyxy = _tolist(getattr(boxes, "xyxy", []))
                scores = _flat_list(getattr(boxes, "conf", []))
                classes = _flat_list(getattr(boxes, "cls", []))
                result_names = _class_mapping(getattr(result, "names", None))
                names = result_names or self.class_mapping
                for index, box in enumerate(xyxy):
                    if len(box) < 4:
                        continue
                    class_id = int(classes[index]) if index < len(classes) else None
                    label_name = names.get(str(class_id)) if class_id is not None else None
                    score = (
                        float(scores[index])
                        if index < len(scores)
                        else self.confidence
                    )
                    detection = {
                        "bbox": [float(value) for value in box[:4]],
                        "class_id": class_id,
                        "class_name": label_name,
                        "confidence": score,
                        "accepted": label_name == self.class_name,
                    }
                    raw_detections.append(detection)
                    if label_name != self.class_name:
                        continue
                    accepted_detections.append(detection)
                    candidates.append(
                        LabelCandidate(
                            bbox=tuple(float(value) for value in box[:4]),  # type: ignore[arg-type]
                            confidence=score,
                            detector=self.name,
                            frame_id=frame_id,
                        )
                    )
            inference_ms = (time.perf_counter() - started) * 1000.0
            self.last_debug = {
                **self.runtime_metadata,
                "event_frame_id": frame_id,
                "inference_ms": inference_ms,
                "raw_detection_count": len(raw_detections),
                "accepted_detection_count": len(accepted_detections),
                "raw_detections": raw_detections,
                "accepted_detections": accepted_detections,
                "state": "SUCCESS",
            }
            return candidates
        except Exception as exc:
            inference_ms = (time.perf_counter() - started) * 1000.0
            self.last_debug = {
                **self.runtime_metadata,
                "event_frame_id": frame_id,
                "inference_ms": inference_ms,
                "raw_detection_count": len(raw_detections),
                "accepted_detection_count": len(accepted_detections),
                "raw_detections": raw_detections,
                "accepted_detections": accepted_detections,
                "state": "FAILED",
                "error": str(exc),
            }
            raise

    def _predict_kwargs(self, *, source: object) -> dict[str, Any]:
        return {
            "source": source,
            "imgsz": self.image_size,
            "conf": self.confidence,
            "iou": self.iou,
            "max_det": self.max_det,
            "device": self.actual_device,
            "verbose": False,
        }


def _validate_detector_options(
    confidence: float, iou: float, image_size: int, max_det: int
) -> None:
    for name, value in (("confidence", confidence), ("iou", iou)):
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ValueError(f"detector {name} must be between 0 and 1")
    if isinstance(image_size, bool) or not isinstance(image_size, int) or image_size < 1:
        raise ValueError("detector image_size must be a positive integer")
    if isinstance(max_det, bool) or not isinstance(max_det, int) or not 1 <= max_det <= 1000:
        raise ValueError("detector max_det must be between 1 and 1000")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _torch_versions() -> tuple[str | None, str | None]:
    try:
        import torch
    except ImportError:  # pragma: no cover - torch is an Ultralytics dependency
        return None, None
    return getattr(torch, "__version__", None), getattr(
        getattr(torch, "version", None), "cuda", None
    )


def _cuda_device_name(actual_device: str) -> str | None:
    if not actual_device.startswith("cuda:"):
        return None
    try:
        import torch

        return str(torch.cuda.get_device_name(int(actual_device.split(":", 1)[1])))
    except (ImportError, AttributeError, RuntimeError, ValueError):
        return None


def _verify_actual_device(device: str) -> str:
    lowered = device.lower()
    if lowered == "cpu":
        return "cpu"
    if lowered.isdigit():
        device = f"cuda:{lowered}"
        lowered = device
    if lowered == "cuda":
        device = "cuda:0"
        lowered = device
    if not lowered.startswith("cuda:"):
        raise ValueError(f"unsupported detector device: {device}")
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - torch is an Ultralytics dependency
        raise RuntimeError("CUDA_UNAVAILABLE: torch is not installed") from exc
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA_UNAVAILABLE: configured detector device cannot run")
    try:
        index = int(device.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"invalid CUDA detector device: {device}") from exc
    if index < 0 or index >= torch.cuda.device_count():
        raise RuntimeError(f"CUDA_DEVICE_UNAVAILABLE: {device}")
    return f"cuda:{index}"


def _class_mapping(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(name) for key, name in value.items()}
    if isinstance(value, (list, tuple)):
        return {str(index): str(name) for index, name in enumerate(value)}
    return {}


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


def _normalize_device(device: str) -> str:
    value = str(device).strip()
    lowered = value.lower()
    if lowered == "gpu":
        return "cuda:0"
    if lowered.startswith("gpu:"):
        return "cuda:" + value.split(":", 1)[1]
    if lowered.isdigit():
        return "cuda:" + lowered
    return value
