"""In-process inspection pipeline.

The compatibility pipeline is imported lazily so station-only modules can use
ranking/preparation types without importing OCR or barcode runtimes.
"""

from __future__ import annotations

from typing import Any


__all__ = ["InspectionPipeline"]


def __getattr__(name: str) -> Any:
    if name == "InspectionPipeline":
        from .inspection import InspectionPipeline

        return InspectionPipeline
    raise AttributeError(name)
