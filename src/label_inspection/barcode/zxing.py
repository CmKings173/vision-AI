"""ZXing-C++ barcode adapter with lightweight preprocessing variants."""

from __future__ import annotations

from typing import Any

from ..schemas import BarcodeResult


class ZXingBarcodeDecoder:
    """Use ``zxing-cpp`` when available; never mix barcode parsing with OCR."""

    def __init__(self, *, use_variants: bool = True) -> None:
        self.use_variants = use_variants
        self._zxing = None

    @property
    def ready(self) -> bool:
        return self._zxing is not None

    def prepare(self) -> bool:
        """Load ZXing-C++ before the runtime announces SYSTEM READY."""

        return self._load() is not None

    def _load(self):
        if self._zxing is None:
            try:
                import zxingcpp
            except Exception:  # import/link errors must stay inside the adapter boundary
                return None
            self._zxing = zxingcpp
        return self._zxing

    def decode(self, image: object) -> list[BarcodeResult]:
        zxing = self._load()
        if zxing is None:
            return [
                BarcodeResult(
                    value=None,
                    error="ZXING_NOT_INSTALLED",
                    success=False,
                    error_code="BARCODE_DEPENDENCY_MISSING",
                    error_message="ZXing-C++ runtime is unavailable.",
                )
            ]

        results: list[BarcodeResult] = []
        for variant in _image_variants(image, enabled=self.use_variants):
            try:
                decoded = zxing.read_barcodes(variant)
            except Exception:  # pragma: no cover - decoder dependent
                if not results:
                    results.append(
                        BarcodeResult(
                            value=None,
                            error="BARCODE_RUNTIME_ERROR",
                            success=False,
                            error_code="BARCODE_RUNTIME_ERROR",
                            error_message="ZXing-C++ decoding failed.",
                        )
                    )
                continue
            for item in decoded or []:
                result = _normalize_result(item)
                if result.value:
                    results.append(result)
            # Run every enabled variant so a crop containing both a 1D code
            # and a QR code keeps both pieces of evidence after deduplication.
        return _unique(results) or [BarcodeResult(value=None)]


def _image_variants(image: object, *, enabled: bool) -> list[object]:
    if not enabled:
        return [image]
    variants = [image]
    try:
        import cv2
        import numpy as np

        array = np.asarray(image)
        if array.ndim == 3:
            gray = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
        else:
            gray = array
        variants.append(gray)
        variants.append(cv2.equalizeHist(gray))
    except (ImportError, TypeError, ValueError, AttributeError):
        pass
    return variants


def _normalize_result(item: Any) -> BarcodeResult:
    value = getattr(item, "text", None) or getattr(item, "content", None)
    format_value = getattr(item, "format", None)
    if format_value is not None and hasattr(format_value, "name"):
        format_value = format_value.name
    position = _normalize_position(getattr(item, "position", None))
    valid = getattr(item, "valid", None)
    if callable(valid):
        valid = valid()
    return BarcodeResult(
        value=str(value) if value is not None else None,
        format=str(format_value) if format_value is not None else None,
        confidence=1.0 if value else 0.0,
        valid=bool(valid) if valid is not None else bool(value),
        position=position,
    )


def _normalize_position(position: Any) -> dict[str, list[float]] | None:
    """Convert zxingcpp.Position/Point objects to JSON-only primitives."""

    if position is None:
        return None
    normalized: dict[str, list[float]] = {}
    for name in ("top_left", "top_right", "bottom_right", "bottom_left"):
        point = getattr(position, name, None)
        if point is None and isinstance(position, dict):
            point = position.get(name)
        coordinates = _normalize_point(point)
        if coordinates is not None:
            normalized[name] = coordinates
    return normalized or None


def _normalize_point(point: Any) -> list[float] | None:
    if point is None:
        return None
    if isinstance(point, (tuple, list)) and len(point) >= 2:
        x, y = point[0], point[1]
    elif isinstance(point, dict):
        x, y = point.get("x"), point.get("y")
    else:
        x, y = getattr(point, "x", None), getattr(point, "y", None)
    if x is None or y is None:
        return None
    try:
        return [float(x), float(y)]
    except (TypeError, ValueError):
        return None


def _unique(results: list[BarcodeResult]) -> list[BarcodeResult]:
    seen: set[tuple[str | None, str | None]] = set()
    unique: list[BarcodeResult] = []
    for result in results:
        key = (result.value, result.format)
        if key in seen:
            continue
        seen.add(key)
        unique.append(result)
    return unique
