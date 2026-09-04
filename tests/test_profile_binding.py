import logging

import pytest

from label_inspection.contracts.profile import (
    APPROVED_FOR_AUTOMATED_PASS,
    UNAPPROVED,
    DocumentRecognitionResult,
    ProfileBinding,
)
from label_inspection.extraction.fields import FieldExtractor
from label_inspection.extraction.profiles import normalize_profile
from label_inspection.schemas import BarcodeResult, QualityReport, RawOCRResult
from label_inspection.validation.rules import LabelValidator
from label_inspection.worker.processor import InspectionProcessor


def test_named_profile_is_unapproved_without_explicit_approval():
    binding = ProfileBinding(name="customer_a", version="1.0")

    assert binding.approval_status == UNAPPROVED
    assert binding.allows_automated_pass is False


def test_only_explicit_approval_allows_automated_pass():
    binding = ProfileBinding(
        name="customer_a",
        version="1.0",
        approval_status=APPROVED_FOR_AUTOMATED_PASS,
    )

    assert binding.allows_automated_pass is True


def test_document_recognition_result_is_explicit_and_profile_bound():
    binding = ProfileBinding(
        name="customer_a",
        version="1.0",
        approval_status=APPROVED_FOR_AUTOMATED_PASS,
    )

    recognition = DocumentRecognitionResult.known(binding)

    assert recognition.is_known is True
    assert recognition.to_dict()["profile_binding"] == binding.to_dict()


def test_processor_rejects_document_recognition_for_a_different_profile():
    active = ProfileBinding(
        name="customer_a",
        version="1.0",
        approval_status=APPROVED_FOR_AUTOMATED_PASS,
    )
    other = ProfileBinding(
        name="customer_b",
        version="1.0",
        approval_status=APPROVED_FOR_AUTOMATED_PASS,
    )

    with pytest.raises(ValueError, match="does not match processor"):
        InspectionProcessor(
            ocr=object(),
            extractor=FieldExtractor(
                fields=("sku",),
                profile_binding=active,
            ),
            validator=LabelValidator(
                required_fields=("sku",),
                profile_binding=active,
            ),
            document_recognition=DocumentRecognitionResult.known(other),
        )


def test_profile_free_binding_cannot_be_approved():
    with pytest.raises(ValueError, match="profile-free binding cannot be approved"):
        ProfileBinding(
            approval_status=APPROVED_FOR_AUTOMATED_PASS,
        )


def test_default_profile_alias_is_deprecated_with_a_warning(caplog):
    caplog.set_level(logging.WARNING)

    assert normalize_profile("default") is None
    assert "deprecated" in caplog.text.lower()


@pytest.mark.parametrize(
    ("name", "version"),
    [("customer_a", None), (None, "1.0")],
)
def test_profile_identity_requires_name_and_version_together(name, version):
    with pytest.raises(ValueError, match="name and version must be provided together"):
        ProfileBinding(name=name, version=version)


def test_binding_normalizes_identity_and_serializes_explicit_approval():
    binding = ProfileBinding(
        name=" Customer-A ",
        version=" 1.0 ",
    )

    assert binding.name == "customer_a"
    assert binding.version == "1.0"
    assert binding.to_dict() == {
        "name": "customer_a",
        "version": "1.0",
        "approval_status": UNAPPROVED,
    }


def test_profile_version_is_trimmed_but_preserves_exact_identity():
    binding = ProfileBinding(
        name=" Customer-A ",
        version=" 2026-09-04 ",
    )

    assert binding.name == "customer_a"
    assert binding.version == "2026-09-04"


def test_named_extractor_without_blockers_is_still_unapproved():
    extractor = FieldExtractor(
        fields=("sku",),
        profile_name="customer_a",
        profile_version="1.0",
        semantic_blockers={},
    )

    assert extractor.profile_approved is False
    assert extractor.profile_binding.approval_status == UNAPPROVED


def test_unapproved_validator_binding_cannot_be_mutated_into_approval():
    validator = LabelValidator(
        profile_binding=ProfileBinding(name="customer_a", version="1.0")
    )

    with pytest.raises(AttributeError):
        validator.profile_approved = True

    result = validator.validate(
        {},
        BarcodeResult(value=None),
        QualityReport(status="PASS"),
        RawOCRResult(engine="test"),
    )

    assert result.status == "REVIEW"



def test_processor_rejects_mismatched_extractor_and_validator_bindings():
    with pytest.raises(ValueError, match="profile binding mismatch"):
        InspectionProcessor(
            ocr=object(),
            extractor=FieldExtractor(
                fields=("sku",),
                profile_name="customer_a",
                profile_version="1.0",
            ),
            validator=LabelValidator(
                required_fields=("sku",),
                profile_name="customer_b",
                profile_version="1.0",
                profile_approved=True,
            ),
        )
