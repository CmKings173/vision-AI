"""PaddleOCR adapter with load-once resident model lifecycle."""

from __future__ import annotations

import time
from typing import Any

from ..schemas import OCRLine, RawOCRResult


class PPOCRAdapter:
    """Fast-path PP-OCR adapter supporting current and legacy result shapes."""

    engine = "ppocr"

    def __init__(self, *, lang: str = "en", device: str = "cpu") -> None:
        self.lang = lang
        self.device = device
        self._ocr = None
        self._load_error: str | None = None

    def _load(self):
        if self._ocr is not None:
            return self._ocr
        if self._load_error is not None:
            return None
        try:
            from paddleocr import PaddleOCR
        except ImportError:  # pragma: no cover - optional runtime dependency
            self._load_error = "PPOCR_DEPENDENCY_MISSING"
            return None
        try:
            self._ocr = PaddleOCR(
                lang=self.lang,
                device=self.device,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except Exception:  # pragma: no cover - optional runtime dependency
            self._load_error = "PPOCR_LOAD_ERROR"
        return self._ocr

    def recognize(self, image: object) -> RawOCRResult:
        started = time.perf_counter()
        ocr = self._load()
        if ocr is None:
            return RawOCRResult(
                engine="ppocr",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=False,
                error=self._load_error or "PPOCR_NOT_AVAILABLE",
                error_code=self._load_error or "PPOCR_NOT_AVAILABLE",
                error_message="PP-OCR runtime is unavailable.",
            )
        try:
            if hasattr(ocr, "predict"):
                output = ocr.predict(image)
            else:
                output = ocr.ocr(image, cls=True)
            lines = normalize_paddle_result(output)
            return RawOCRResult(
                engine="ppocr",
                lines=lines,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=True,
                raw={"line_count": len(lines)},
            )
        except Exception:  # pragma: no cover - optional runtime dependency
            return RawOCRResult(
                engine="ppocr",
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=False,
                error="OCR_RUNTIME_ERROR",
                error_code="OCR_RUNTIME_ERROR",
                error_message="PP-OCR inference failed.",
            )


def normalize_paddle_result(output: Any) -> list[OCRLine]:
    """Normalize PaddleOCR 3.x result objects and legacy nested output."""

    lines: list[OCRLine] = []
    for item in _as_items(output):
        texts = _get(item, "rec_texts")
        scores = _get(item, "rec_scores")
        polygons = _get(item, "rec_polys")
        if polygons is None:
            polygons = _get(item, "dt_polys")
        if polygons is None:
            polygons = _get(item, "rec_boxes")
        if texts is not None:
            for index, text in enumerate(_as_list(texts)):
                score_values = _as_list(scores)
                polygon_values = _as_list(polygons)
                lines.append(
                    OCRLine(
                        text=str(text),
                        confidence=float(score_values[index]) if index < len(score_values) else 0.0,
                        polygon=_polygon(polygon_values[index]) if index < len(polygon_values) else None,
                    )
                )
            continue
        if _legacy_line(item):
            text, confidence, polygon = _legacy_line(item)
            lines.append(OCRLine(text=text, confidence=confidence, polygon=polygon))
    return [line for line in lines if line.text.strip()]


def _as_items(output: Any) -> list[Any]:
    if output is None:
        return []
    if isinstance(output, dict) or hasattr(output, "rec_texts"):
        return [output]
    if isinstance(output, (list, tuple)):
        # Legacy output is often [[ [box, (text, score)], ... ]].
        if output and isinstance(output[0], (list, tuple)):
            first = output[0]
            if first and _legacy_line(first[0]):
                return list(first)
        return list(output)
    if hasattr(output, "__iter__") and not isinstance(output, (str, bytes)):
        try:
            return list(output)
        except TypeError:
            return []
    return []


def _get(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _polygon(value: Any) -> list[list[float]] | None:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    try:
        if len(value) == 4 and all(
            isinstance(coordinate, (int, float)) for coordinate in value
        ):
            x1, y1, x2, y2 = (float(coordinate) for coordinate in value)
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        return [[float(point[0]), float(point[1])] for point in value]
    except (TypeError, IndexError, ValueError):
        return None


def _legacy_line(value: Any):
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    candidate = value[1]
    if not isinstance(candidate, (list, tuple)) or len(candidate) < 2:
        return None
    text, confidence = candidate[0], candidate[1]
    if not isinstance(text, str):
        return None
    return text, float(confidence), _polygon(value[0])
