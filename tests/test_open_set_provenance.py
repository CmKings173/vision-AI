from types import SimpleNamespace

import pytest

from label_inspection.worker.provenance import WorkerRuntimeDescriptor


def _descriptor(*, profile_name=None, profile_version=None):
    processor = SimpleNamespace(
        ocr=SimpleNamespace(engine="ocr", version="1"),
        barcode=SimpleNamespace(engine="barcode", version="1"),
        extractor=SimpleNamespace(
            profile_name=profile_name,
            profile_version=profile_version,
            mapping_summary={},
            semantic_blockers={},
        ),
        validator=SimpleNamespace(
            required_fields=(),
            barcode_required=False,
            profile_name=profile_name,
            profile_version=profile_version,
            profile_approved=profile_name is not None,
        ),
    )
    return WorkerRuntimeDescriptor.from_processor(processor)


def test_profile_free_worker_accepts_profile_free_request():
    descriptor = _descriptor()

    descriptor.assert_compatible({"requested_profile": None})


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
