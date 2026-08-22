"""Resident PP-OCRv6 Transformers adapter for the GX10 runtime."""

from __future__ import annotations

import importlib
import time

from ..schemas import RawOCRResult
from .ppocr import normalize_paddle_result


class PPOCRV6TransformersAdapter:
    """Load PP-OCRv6 once and reuse the Transformers-backed predictor."""

    engine = "ppocr_v6"
    backend = "transformers"

    def __init__(
        self,
        *,
        device: str = "gpu:0",
        ocr_version: str = "PP-OCRv6",
    ) -> None:
        self.device = "gpu:0" if device.strip().lower() == "gpu" else device
        self.ocr_version = ocr_version
        self._ocr = None
        self._load_error: str | None = None

    def _load(self):
        if self._ocr is not None:
            return self._ocr
        if self._load_error is not None:
            return None
        try:
            paddleocr = importlib.import_module("paddleocr")
            paddle_ocr = getattr(paddleocr, "PaddleOCR")
        except ImportError:
            self._load_error = "PP-OCRV6_DEPENDENCY_MISSING"
            return None
        except Exception:
            self._load_error = "PP-OCRV6_IMPORT_ERROR"
            return None
        try:
            self._ocr = paddle_ocr(
                ocr_version=self.ocr_version,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                engine=self.backend,
                device=self.device,
            )
        except Exception:
            self._load_error = "PP-OCRV6_LOAD_ERROR"
        return self._ocr

    def recognize(self, image: object) -> RawOCRResult:
        started = time.perf_counter()
        ocr = self._load()
        if ocr is None:
            return RawOCRResult(
                engine=self.engine,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=False,
                error=self._load_error or "PP-OCRV6_NOT_AVAILABLE",
                error_code=self._load_error or "PP-OCRV6_NOT_AVAILABLE",
                error_message="PP-OCRv6 Transformers runtime is unavailable.",
                backend=self.backend,
                device=self.device,
                model=self.ocr_version,
            )
        try:
            if not hasattr(ocr, "predict"):
                raise RuntimeError("PaddleOCR.predict is required for PP-OCRv6")
            lines = normalize_paddle_result(ocr.predict(image))
            return RawOCRResult(
                engine=self.engine,
                lines=lines,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=True,
                raw={
                    "line_count": len(lines),
                    "backend": self.backend,
                    "device": self.device,
                    "model": self.ocr_version,
                },
                backend=self.backend,
                device=self.device,
                model=self.ocr_version,
            )
        except Exception:
            return RawOCRResult(
                engine=self.engine,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                success=False,
                error="PP-OCRV6_RUNTIME_ERROR",
                error_code="PP-OCRV6_RUNTIME_ERROR",
                error_message="PP-OCRv6 Transformers inference failed.",
                backend=self.backend,
                device=self.device,
                model=self.ocr_version,
            )
