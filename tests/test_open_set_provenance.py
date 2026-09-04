from types import SimpleNamespace

import pytest

from label_inspection.contracts import DocumentRecognitionResult, ProfileBinding
from label_inspection.worker.provenance import WorkerRuntimeDescriptor


def _descriptor(*, profile_name=None, profile_version=None, recognition_status=None):
    binding = ProfileBinding.from_legacy(
        name=profile_name,
        version=profile_version,
        approved=profile_name is not None,
    )
    processor = SimpleNamespace(
        profile_binding=binding,
        ocr=SimpleNamespace(engine="ocr", version="1"),
        barcode=SimpleNamespace(engine="barcode", version="1"),
        extractor=SimpleNamespace(
            profile_name=profile_name,
            profile_version=profile_version,
            mapping_summary={},
            semantic_blockers={},
            profile_binding=binding,
        ),
        validator=SimpleNamespace(
            required_fields=(),
            barcode_required=False,
            profile_name=profile_name,
            profile_version=profile_version,
            profile_approved=profile_name is not None,
            profile_binding=binding,
        ),
        document_recognition=(
            None
            if recognition_status is None
            else DocumentRecognitionResult(
                status=recognition_status,
                profile_binding=binding,
                reason=f"{recognition_status.lower()} document",
            )
        ),
    )
    return WorkerRuntimeDescriptor.from_processor(processor)


def test_profile_free_worker_accepts_profile_free_request():
    descriptor = _descriptor()

    descriptor.assert_compatible({"requested_profile": None})
    assert descriptor.to_dict()["document_recognition"] is None


def test_named_worker_rejects_profile_free_request():
    descriptor = _descriptor(profile_name="dgx_spark_label", profile_version="1.0")

    with pytest.raises(ValueError, match="profile-free request"):
        descriptor.assert_compatible({"requested_profile": None})


def test_profile_free_worker_rejects_named_request():
    descriptor = _descriptor()

    with pytest.raises(ValueError, match="profile-free worker"):
        descriptor.assert_compatible(
            {"requested_profile": {"name": "dgx_spark_label", "version": "1.0"}}
        )


def test_profile_free_worker_accepts_legacy_default_as_safe_profile_free_alias():
    descriptor = _descriptor()

    descriptor.assert_compatible(
        {"requested_profile": {"name": "default", "version": "1.0"}}
    )


def test_runtime_descriptor_exposes_one_shared_profile_binding():
    descriptor = _descriptor(profile_name="customer_a", profile_version="1.0")

    assert descriptor.profile == {
        "name": "customer_a",
        "version": "1.0",
        "approval_status": "APPROVED_FOR_AUTOMATED_PASS",
    }


def test_runtime_descriptor_metadata_is_immutable():
    descriptor = _descriptor(profile_name="customer_a", profile_version="1.0")

    with pytest.raises(TypeError):
        descriptor.profile["name"] = "customer_b"


def test_runtime_descriptor_rejects_unknown_profile_contract_version():
    descriptor = _descriptor()

    with pytest.raises(ValueError, match="unsupported profile contract version"):
        descriptor.assert_compatible(
            {
                "profile_contract_version": "profile-binding.v1",
                "requested_profile": None,
            }
        )


@pytest.mark.parametrize("recognition_status", ["KNOWN", "UNKNOWN", "AMBIGUOUS"])
def test_runtime_descriptor_persists_full_document_recognition_contract(
    recognition_status,
):
    descriptor = _descriptor(
        profile_name="customer_a",
        profile_version="2026-09-04",
        recognition_status=recognition_status,
    )

    serialized = descriptor.to_dict()

    assert serialized["document_recognition"] == {
        "status": recognition_status,
        "profile_binding": {
            "name": "customer_a",
            "version": "2026-09-04",
            "approval_status": "APPROVED_FOR_AUTOMATED_PASS",
        },
        "reason": f"{recognition_status.lower()} document",
    }
    assert serialized["trusted_document_recognition"] is (
        recognition_status == "KNOWN"
    )


def test_runtime_descriptor_preserves_profile_version_for_compatibility():
    descriptor = _descriptor(
        profile_name="customer_a",
        profile_version="2026-09-04",
    )

    descriptor.assert_compatible(
        {
            "requested_profile": {
                "name": "customer_a",
                "version": "2026-09-04",
            }
        }
    )
