"""Resident OCR adapters."""

from .base import OCRProvider
from .ppocr import PPOCRAdapter, normalize_paddle_result

__all__ = ["OCRProvider", "PPOCRAdapter", "normalize_paddle_result"]
