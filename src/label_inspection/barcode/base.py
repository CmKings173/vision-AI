"""Barcode decoder contract."""

from __future__ import annotations

from typing import Protocol

from ..schemas import BarcodeResult


class BarcodeDecoder(Protocol):
    def decode(self, image: object) -> list[BarcodeResult]:
        """Decode zero or more barcodes from a crop."""


class NullBarcodeDecoder:
    """Explicit no-op decoder for tests or deployments without barcode hardware."""

    def decode(self, image: object) -> list[BarcodeResult]:
        return []
