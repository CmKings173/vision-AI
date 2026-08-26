"""Actual worker runtime provenance and requested-profile compatibility."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts.core import require_text
from .processor import InspectionProcessor

WORKER_PIPELINE_VERSION = "phase2-worker.v1"


@dataclass(frozen=True)
class WorkerRuntimeDescriptor:
    pipeline_version: str
    ocr: Mapping[str, Any]
    barcode: Mapping[str, Any]
    extractor: Mapping[str, Any]
    validator: Mapping[str, Any]

    @classmethod
    def from_processor(cls, processor: InspectionProcessor) -> WorkerRuntimeDescriptor:
        ocr = processor.ocr
        barcode = processor.barcode
        extractor = processor.extractor
        validator = processor.validator
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
            extractor={
                **_component_descriptor(extractor),
                "profile_name": str(
                    getattr(extractor, "profile_name", "unknown")
                ),
                "profile_version": str(
                    getattr(extractor, "profile_version", "unknown")
                ),
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
            },
        )

    def assert_compatible(self, provenance: Mapping[str, Any]) -> None:
        requested = provenance.get("requested_profile")
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
        active_name = _normalize_profile(str(self.extractor["profile_name"]))
        active_version = str(self.extractor["profile_version"])
        if (requested_name, requested_version) != (active_name, active_version):
            raise ValueError("requested profile is incompatible with worker runtime")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_version": self.pipeline_version,
            "ocr": dict(self.ocr),
            "barcode": dict(self.barcode),
            "extractor": dict(self.extractor),
            "validator": dict(self.validator),
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
