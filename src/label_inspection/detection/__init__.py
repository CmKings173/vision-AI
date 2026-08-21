"""Label candidate detectors."""

from .base import LabelDetector
from .fixed_roi import FixedROIDetector

__all__ = ["FixedROIDetector", "LabelDetector"]
