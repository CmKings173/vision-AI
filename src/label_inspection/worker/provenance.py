"""Actual worker runtime provenance and requested-profile compatibility."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts.core import freeze_json, require_text, thaw_json
from ..contracts.profile import (
    APPROVED_FOR_AUTOMATED_PASS,
    PROFILE_BINDING_VERSION,
    DocumentRecognitionResult,
    ProfileBinding,
)
from .processor import InspectionProcessor

WORKER_PIPELINE_VERSION = "phase2-worker.v2"


@dataclass(frozen=True)
class WorkerRuntimeDescriptor:
    pipeline_version: str
    ocr: Mapping[str, Any]
    barcode: Mapping[str, Any]
    profile: Mapping[str, Any]
    extractor: Mapping[str, Any]
    validator: Mapping[str, Any]
    document_recognition: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("ocr", "barcode", "profile", "extractor", "validator"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        if self.document_recognition is not None:
            object.__setattr__(
                self,
                "document_recognition",
                freeze_json(self.document_recognition),
            )

    @property
    def trusted_document_recognition(self) -> bool:
        """Return the compatibility flag derived from the recognition contract."""

        return bool(
            self.document_recognition is not None
            and self.document_recognition.get("status") == "KNOWN"
        )

    @property
    def allows_automated_business_decision(self) -> bool:
        """Authorize PASS/FAIL only for one approved, recognized profile."""

        return bool(
            self.profile.get("approval_status")
            == APPROVED_FOR_AUTOMATED_PASS
            and self.document_recognition is not None
            and self.document_recognition.get("status") == "KNOWN"
            and self.document_recognition.get("profile_binding") == self.profile
        )

    @property
    def semantic_review_reason(self) -> str:
        if self.profile.get("approval_status") != APPROVED_FOR_AUTOMATED_PASS:
            return "NO_APPROVED_PROFILE"
        return "NO_TRUSTED_DOCUMENT_RECOGNITION"

    @classmethod
    def from_processor(cls, processor: InspectionProcessor) -> WorkerRuntimeDescriptor:
        ocr = processor.ocr
        barcode = processor.barcode
        extractor = processor.extractor
        validator = processor.validator
        extractor_binding = getattr(extractor, "profile_binding", None)
        validator_binding = getattr(validator, "profile_binding", None)
        processor_binding = getattr(processor, "profile_binding", None)
        if not all(
            isinstance(binding, ProfileBinding)
            for binding in (extractor_binding, validator_binding, processor_binding)
        ):
            raise ValueError("processor components must expose a ProfileBinding")
        if not (
            extractor_binding == validator_binding == processor_binding
        ):
            raise ValueError("processor profile bindings are inconsistent")
        document_recognition = getattr(processor, "document_recognition", None)
        if document_recognition is not None and not isinstance(
            document_recognition, DocumentRecognitionResult
        ):
            raise ValueError("processor document recognition contract is invalid")
        if (
            document_recognition is not None
            and document_recognition.profile_binding != processor_binding
        ):
            raise ValueError("processor document recognition binding is inconsistent")
        return cls(
            pipeline_version=WORKER_PIPELINE_VERSION,
            ocr=_component_descriptor(
                ocr,
                engine=getattr(ocr, "engine", "unknown"),
                version=getattr(
                    ocr,
                    "ocr_version",
                    getattr(ocr, "version", "unknown"),
                ),
            ),
            barcode=_component_descriptor(
                barcode,
                engine=getattr(barcode, "engine", "zxing-cpp"),
                version=getattr(barcode, "version", "unknown"),
            ),
            profile=processor_binding.to_dict(),
            extractor={
                **_component_descriptor(extractor),
                "profile_name": getattr(extractor, "profile_name", None),
                "profile_version": getattr(extractor, "profile_version", None),
                "mapping_summary": dict(
                    getattr(extractor, "mapping_summary", {})
                ),
                "semantic_blockers": dict(
                    getattr(extractor, "semantic_blockers", {})
                ),
            },
            validator={
                **_component_descriptor(
                    validator,
                    version=getattr(validator, "profile_version", "1.0"),
                ),
                "required_fields": list(
                    getattr(validator, "required_fields", ())
                ),
                "barcode_required": bool(
                    getattr(validator, "barcode_required", False)
                ),
                "profile_name": getattr(validator, "profile_name", None),
                "profile_version": getattr(validator, "profile_version", None),
                "profile_approved": bool(
                    getattr(validator, "profile_approved", False)
                ),
            },
            document_recognition=(
                None
                if document_recognition is None
                else document_recognition.to_dict()
            ),
        )

    def assert_compatible(self, provenance: Mapping[str, Any]) -> None:
        contract_version = provenance.get("profile_contract_version")
        if contract_version is not None and contract_version != PROFILE_BINDING_VERSION:
            raise ValueError("unsupported profile contract version")
        if "requested_profile" not in provenance:
            raise ValueError("requested_profile is required")
        requested = provenance.get("requested_profile")
        active_name = self.profile.get("name")
        active_version = self.profile.get("version")
        if requested is None:
            if active_name is not None or active_version is not None:
                raise ValueError(
                    "profile-free request is incompatible with a named worker profile"
                )
            return
        if not isinstance(requested, Mapping) or set(requested) != {
            "name",
            "version",
        }:
            raise ValueError("requested_profile must contain name and version")
        requested_name = _normalize_profile(
            require_text(requested["name"], "requested profile name")
        )
        requested_version = require_text(
            requested["version"], "requested profile version"
        )
        if (
            active_name is None
            and requested_name == "default"
            and requested_version == "1.0"
        ):
            # The old station used default/1.0 for the implicit profile. This
            # migration alias is safe because a v2 profile-free worker still
            # emits evidence-only REVIEW results.
            return
        if active_name is None or active_version is None:
            raise ValueError(
                "named profile request is incompatible with a profile-free worker"
            )
        active_name = _normalize_profile(str(active_name))
        active_version = str(active_version)
        if (requested_name, requested_version) != (active_name, active_version):
            raise ValueError("requested profile is incompatible with worker runtime")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_version": self.pipeline_version,
            "ocr": thaw_json(self.ocr),
            "barcode": thaw_json(self.barcode),
            "profile": thaw_json(self.profile),
            "extractor": thaw_json(self.extractor),
            "validator": thaw_json(self.validator),
            "document_recognition": (
                None
                if self.document_recognition is None
                else thaw_json(self.document_recognition)
            ),
            # Retain the old derived flag for readers that have not yet moved
            # to the complete recognition contract above.
            "trusted_document_recognition": self.trusted_document_recognition,
        }


def _component_descriptor(
    component: object,
    *,
    engine: object | None = None,
    version: object | None = None,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "implementation": type(component).__name__,
        "module": type(component).__module__,
    }
    if engine is not None:
        descriptor["engine"] = str(engine)
    if version is not None:
        descriptor["version"] = str(version)
    return descriptor


def _normalize_profile(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    return "dgx_spark_label" if normalized == "dgx_spark" else normalized
