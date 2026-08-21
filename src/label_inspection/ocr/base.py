"""OCR provider contract."""

from __future__ import annotations

from typing import Protocol

from ..schemas import RawOCRResult


class OCRProvider(Protocol):
    def recognize(self, image: object) -> RawOCRResult:
        """Recognize text without changing the raw lines into business fields."""
