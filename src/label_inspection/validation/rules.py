"""PASS/FAIL/REVIEW/ERROR rules for a label inspection result."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Optional

from ..contracts.profile import ProfileBinding
from ..schemas import (
    BarcodeResult,
    ExtractedField,
    QualityReport,
    RawOCRResult,
    ValidationResult,
)


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
        profile_binding: ProfileBinding | None = None,
    ) -> None:
        self.required_fields = tuple(field.lower() for field in required_fields)
        self.barcode_required = barcode_required
        self.field_patterns = {
            key.lower(): re.compile(pattern) for key, pattern in (field_patterns or {}).items()
        }
        self.min_field_confidence = min_field_confidence
        if profile_binding is None:
            self._profile_binding = ProfileBinding.from_legacy(
                name=profile_name,
                version=profile_version,
                approved=profile_approved,
            )
        else:
            if profile_name is not None or profile_version is not None or profile_approved:
                raise ValueError(
                    "profile binding cannot be combined with legacy profile arguments"
                )
            self._profile_binding = profile_binding

    @property
    def profile_binding(self) -> ProfileBinding:
        """Return the immutable binding used by this validator."""

        return self._profile_binding

    @property
    def profile_name(self) -> str | None:
        return self._profile_binding.name

    @property
    def profile_version(self) -> str | None:
        return self._profile_binding.version

    @property
    def profile_approved(self) -> bool:
        return self._profile_binding.allows_automated_pass

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

        if self.profile_approved:
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
            reasons.append(barcode.error_code or "BARCODE_RUNTIME_ERROR")
            return ValidationResult(status="ERROR", reasons=tuple(reasons))
        elif self.profile_approved and barcode.value and barcode.valid is False:
            hard_fail = True
            reasons.append("BARCODE_INVALID")
        elif self.profile_approved and self.barcode_required and not barcode.value:
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
