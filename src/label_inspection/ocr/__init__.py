"""Resident OCR adapters."""

from .base import OCRProvider
from .ppocr import PPOCRAdapter, normalize_paddle_result
from .tensorrt_ocr import TensorRTOCRAdapter, TensorRTEngineRunner, decode_ctc

__all__ = [
    "OCRProvider",
    "PPOCRAdapter",
    "TensorRTOCRAdapter",
    "TensorRTEngineRunner",
    "decode_ctc",
    "normalize_paddle_result",
]
