"""Deterministic SKU/LOT extraction, kept separate from OCR inference."""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from ..schemas import ExtractedField, OCRLine


DEFAULT_PATTERNS: dict[str, re.Pattern[str]] = {
    "sku": re.compile(
        r"\b(?:SKU|ITEM|PRODUCT(?:\s*CODE)?|CODE)\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
        re.IGNORECASE,
    ),
    "lot": re.compile(
        r"\b(?:LOT\s*NO|BATCH|LOT)\s*[:#=\-]?\s*([A-Z0-9][A-Z0-9._/\-]{1,})",
        re.IGNORECASE,
    ),
    "tracking_number": re.compile(
        r"\b(SPX[A-Z0-9]{8,})\b",
        re.IGNORECASE,
    ),
    "order_id": re.compile(
        r"\b([0-9]{6,}[A-Z0-9]{4,})\b",
        re.IGNORECASE,
    ),
}


class FieldExtractor:
    """Map OCR lines to business fields while preserving source evidence."""

    def __init__(
        self,
        fields: Iterable[str] = ("sku", "lot"),
        *,
        patterns: Mapping[str, re.Pattern[str]] | None = None,
        allow_adjacent_line_values: bool = False,
    ) -> None:
        self.fields = tuple(field.lower() for field in fields)
        self.patterns = dict(DEFAULT_PATTERNS)
        if patterns:
            self.patterns.update({key.lower(): value for key, value in patterns.items()})
        self.allow_adjacent_line_values = allow_adjacent_line_values

    def extract(
        self,
        lines: Iterable[OCRLine],
        *,
        source: str = "ocr",
    ) -> dict[str, ExtractedField]:
        line_list = list(lines)
        extracted = {
            field: ExtractedField(value=None, reason="NOT_FOUND") for field in self.fields
        }
        for field in self.fields:
            pattern = self.patterns.get(field)
            if pattern is None:
                continue
            matches: list[ExtractedField] = []
            candidates = [
                (line.text, float(line.confidence), line.text)
                for line in line_list
            ]
            if self.allow_adjacent_line_values:
                candidates.extend(
                    (
                        f"{line.text} {next_line.text}",
                        min(float(line.confidence), float(next_line.confidence)),
                        f"{line.text} {next_line.text}",
                    )
                    for line, next_line in zip(line_list, line_list[1:])
                )
            for text, confidence, line_text in candidates:
                match = pattern.search(text)
                if not match:
                    continue
                value = match.group(1).strip(" .,:;|[]()")
                if not value:
                    continue
                matches.append(
                    ExtractedField(
                        value=value,
                        confidence=max(0.0, min(1.0, confidence)),
                        source=source,
                        line_text=line_text,
                    )
                )
            if matches:
                extracted[field] = max(matches, key=lambda item: item.confidence)
        return extracted
