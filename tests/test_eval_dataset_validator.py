import csv
import json
from pathlib import Path

from label_inspection.evaluation.dataset import validate_dataset


FIELDS = (
    "customer_part_number",
    "so_number",
    "our_part_number",
    "quantity",
    "net_weight",
    "gross_weight",
    "carton_number",
    "datamatrix",
    "expected_business_status",
)


def _sample(
    image_id="IMG_001",
    *,
    group="GROUP_001",
    image_path="images/IMG_001.jpg",
    split="smoke",
    human_verified=False,
    expected=None,
    conditions=None,
):
    expected = expected or {field: None for field in FIELDS}
    return {
        "schema_version": "1.0",
        "image_id": image_id,
        "inspection_group_id": group,
        "image_path": image_path,
        "split": split,
        "human_verified": human_verified,
        "expected": expected,
        "conditions": conditions
        or {
            "lighting": "unknown",
            "glare": None,
            "blur": None,
            "occluded": None,
            "rotation_bucket": "unknown",
            "distance_bucket": "unknown",
            "position_bucket": "unknown",
            "negative_sample": False,
        },
    }


def _write_dataset(tmp_path: Path, samples, *, manifest_rows=None, config=None):
    (tmp_path / "images").mkdir(exist_ok=True)
    for sample in samples:
        image_path = tmp_path / sample["image_path"]
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"VALID")
    (tmp_path / "dataset_config.json").write_text(
        json.dumps(
            config
            or {
                "schema_version": "1.0",
                "dataset_id": "test_dataset",
                "dataset_role": "target",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"schema_version": "1.0", "images": samples}),
        encoding="utf-8",
    )
    rows = manifest_rows
    if rows is None:
        rows = [
            {
                "image_id": sample["image_id"],
                "inspection_group_id": sample["inspection_group_id"],
                "image_path": sample["image_path"],
                "split": sample["split"],
                "human_verified": str(sample["human_verified"]).lower(),
                "source": "test",
                "synthetic": "false",
                "parent_image_id": "",
            }
            for sample in samples
        ]
    with (tmp_path / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "image_id",
                "inspection_group_id",
                "image_path",
                "split",
                "human_verified",
                "source",
                "synthetic",
                "parent_image_id",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)


def _readable(path: Path) -> bool:
    return path.read_bytes() == b"VALID"


def test_valid_unverified_smoke_dataset_is_accepted_with_warning(tmp_path):
    _write_dataset(tmp_path, [_sample()])

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert report.ok
    assert report.summary["dataset_samples"] == 1
    assert report.summary["human_verified_samples"] == 0
    assert any(issue.code == "UNVERIFIED_SAMPLE" for issue in report.warnings)


def test_missing_image_is_error(tmp_path):
    sample = _sample()
    _write_dataset(tmp_path, [sample])
    (tmp_path / sample["image_path"]).unlink()

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "IMAGE_MISSING" for issue in report.errors)


def test_corrupt_image_is_error(tmp_path):
    sample = _sample()
    _write_dataset(tmp_path, [sample])
    (tmp_path / sample["image_path"]).write_bytes(b"CORRUPT")

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "IMAGE_UNREADABLE" for issue in report.errors)


def test_duplicate_image_id_and_path_are_errors(tmp_path):
    samples = [_sample("IMG_001"), _sample("IMG_001", image_path="images/IMG_002.jpg")]
    _write_dataset(tmp_path, samples)

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "DUPLICATE_IMAGE_ID" for issue in report.errors)

    samples = [_sample("IMG_001"), _sample("IMG_002", image_path="images/IMG_001.jpg")]
    _write_dataset(tmp_path, samples)
    report = validate_dataset(tmp_path, image_reader=_readable)
    assert any(issue.code == "DUPLICATE_IMAGE_PATH" for issue in report.errors)


def test_invalid_split_and_schema_version_are_errors(tmp_path):
    sample = _sample(split="production")
    _write_dataset(tmp_path, [sample], config={"schema_version": "9.0"})

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "INVALID_SPLIT" for issue in report.errors)
    assert any(issue.code == "UNSUPPORTED_SCHEMA_VERSION" for issue in report.errors)


def test_malformed_json_is_error(tmp_path):
    _write_dataset(tmp_path, [_sample()])
    (tmp_path / "ground_truth.json").write_text("{not-json", encoding="utf-8")

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "MALFORMED_JSON" for issue in report.errors)


def test_human_verified_sample_allows_explicit_null_values(tmp_path):
    sample = _sample(human_verified=True)
    _write_dataset(tmp_path, [sample])

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert report.ok
    assert report.summary["human_verified_samples"] == 1
    assert report.summary["production_verified_samples"] == 1


def test_expected_object_must_contain_every_schema_field(tmp_path):
    expected = {field: None for field in FIELDS}
    del expected["customer_part_number"]
    _write_dataset(tmp_path, [_sample(human_verified=True, expected=expected)])

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "MISSING_EXPECTED_FIELD" for issue in report.errors)


def test_image_id_rejects_path_components_and_non_allowlisted_characters(tmp_path):
    samples = [
        _sample("../../ESCAPE"),
        _sample("DOT.ID", image_path="images/DOT_ID.jpg", group="GROUP_002"),
        _sample("PLUS+ID", image_path="images/PLUS_ID.jpg", group="GROUP_003"),
    ]
    _write_dataset(tmp_path, samples)

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    invalid_ids = {
        issue.image_id
        for issue in report.errors
        if issue.code == "INVALID_IMAGE_ID_FORMAT"
    }
    assert invalid_ids == {"../../ESCAPE", "DOT.ID", "PLUS+ID"}


def test_dataset_role_is_required_and_must_be_supported(tmp_path):
    _write_dataset(
        tmp_path,
        [_sample()],
        config={"schema_version": "1.0", "dataset_id": "role_missing"},
    )
    report = validate_dataset(tmp_path, image_reader=_readable)
    assert not report.ok
    assert any(issue.code == "INVALID_DATASET_ROLE" for issue in report.errors)

    _write_dataset(
        tmp_path,
        [_sample()],
        config={
            "schema_version": "1.0",
            "dataset_id": "role_invalid",
            "dataset_role": "production",
        },
    )
    report = validate_dataset(tmp_path, image_reader=_readable)
    assert not report.ok
    assert any(issue.code == "INVALID_DATASET_ROLE" for issue in report.errors)


def test_malformed_expected_value_is_error(tmp_path):
    expected = {field: None for field in FIELDS}
    expected["quantity"] = "two"
    _write_dataset(tmp_path, [_sample(expected=expected)])

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "INVALID_EXPECTED_TYPE" for issue in report.errors)


def test_group_cannot_span_splits(tmp_path):
    samples = [
        _sample("IMG_001", split="development"),
        _sample("IMG_002", split="validation", image_path="images/IMG_002.jpg"),
    ]
    _write_dataset(tmp_path, samples)

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "GROUP_SPLIT_LEAKAGE" for issue in report.errors)


def test_synthetic_parent_must_share_parent_split(tmp_path):
    parent = _sample("IMG_001", split="development")
    child = _sample(
        "IMG_001_ROT",
        group="GROUP_001",
        image_path="images/IMG_001_ROT.jpg",
        split="validation",
    )
    _write_dataset(
        tmp_path,
        [parent, child],
        manifest_rows=[
            {
                "image_id": "IMG_001",
                "inspection_group_id": "GROUP_001",
                "image_path": "images/IMG_001.jpg",
                "split": "development",
                "human_verified": "false",
                "source": "test",
                "synthetic": "false",
                "parent_image_id": "",
            },
            {
                "image_id": "IMG_001_ROT",
                "inspection_group_id": "GROUP_001",
                "image_path": "images/IMG_001_ROT.jpg",
                "split": "validation",
                "human_verified": "false",
                "source": "test",
                "synthetic": "true",
                "parent_image_id": "IMG_001",
            },
        ],
    )

    report = validate_dataset(tmp_path, image_reader=_readable)

    assert not report.ok
    assert any(issue.code == "SYNTHETIC_PARENT_SPLIT_MISMATCH" for issue in report.errors)
