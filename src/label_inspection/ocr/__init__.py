"""Resident OCR adapters."""

from .base import OCRProvider
from .ppocr import PPOCRAdapter, normalize_paddle_result
from .ppocr_v6 import PPOCRV6TransformersAdapter
from .tensorrt_ocr import TensorRTOCRAdapter, TensorRTEngineRunner, decode_ctc

__all__ = [
    "OCRProvider",
    "PPOCRAdapter",
    "PPOCRV6TransformersAdapter",
    "TensorRTOCRAdapter",
    "TensorRTEngineRunner",
    "decode_ctc",
    "normalize_paddle_result",
]
