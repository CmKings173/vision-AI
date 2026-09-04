"""PASS/FAIL/REVIEW/ERROR rules for a label inspection result."""

from __future__ import annotations

import re
from typing import Mapping, Optional

from ..schemas import BarcodeResult, ExtractedField, QualityReport, RawOCRResult, ValidationResult


class LabelValidator:
    def __init__(
        self,
        *,
        required_fields: tuple[str, ...] = (),
        barcode_required: bool = False,
        field_patterns: Optional[Mapping[str, str]] = None,
        min_field_confidence: float = 0.7,
        profile_name: str | None = None,
        profile_version: str | None = None,
        profile_approved: bool = False,
    ) -> None:
        self.required_fields = tuple(field.lower() for field in required_fields)
        self.barcode_required = barcode_required
        self.field_patterns = {
            key.lower(): re.compile(pattern) for key, pattern in (field_patterns or {}).items()
        }
        self.min_field_confidence = min_field_confidence
        self.profile_name = profile_name.strip() if profile_name and profile_name.strip() else None
        self.profile_version = profile_version.strip() if profile_version and profile_version.strip() else None
        if not isinstance(profile_approved, bool):
            raise ValueError("profile_approved must be boolean")
        self.profile_approved = profile_approved

    def validate(
        self,
        extracted: Mapping[str, ExtractedField],
        barcode: BarcodeResult,
        quality: QualityReport,
        raw_ocr: RawOCRResult,
    ) -> ValidationResult:
        reasons: list[str] = []
        hard_fail = False
        review = False

        if quality.status == "FAIL":
            review = True
            reasons.extend(f"QUALITY_{reason}" for reason in quality.reasons)
        elif quality.status not in {"PASS", ""}:
            reasons.append("QUALITY_ERROR")
            return ValidationResult(status="ERROR", reasons=tuple(reasons))

        if not raw_ocr.success:
            reasons.append(raw_ocr.error_code or "OCR_ERROR")
            return ValidationResult(status="ERROR", reasons=tuple(reasons))

        for field in self.required_fields:
            item = extracted.get(field)
            if item is None or not item.value:
                review = True
                reasons.append(f"MISSING_{field.upper()}")
                continue
            if item.confidence < self.min_field_confidence:
                review = True
                reasons.append(f"LOW_CONFIDENCE_{field.upper()}")
            pattern = self.field_patterns.get(field)
            if pattern and not pattern.fullmatch(item.value):
                hard_fail = True
                reasons.append(f"INVALID_{field.upper()}_FORMAT")

        if not barcode.success:
            review = True
            reasons.append(barcode.error_code or "BARCODE_RUNTIME_ERROR")
        elif barcode.value and barcode.valid is False:
            hard_fail = True
            reasons.append("BARCODE_INVALID")
        elif self.barcode_required and not barcode.value:
            review = True
            reasons.append("MISSING_BARCODE")

        if hard_fail:
            status = "FAIL"
        elif review:
            status = "REVIEW"
        else:
            status = "PASS"

        # Semantic validation is opt-in.  Evidence may be fully available for
        # an unknown document, but it is not safe to turn that evidence into a
        # business PASS without an approved profile.
        if self.profile_name is None or not self.profile_approved:
            if "NO_APPROVED_PROFILE" not in reasons:
                reasons.append("NO_APPROVED_PROFILE")
            if status != "ERROR":
                status = "REVIEW"
        return ValidationResult(status=status, reasons=tuple(reasons))
