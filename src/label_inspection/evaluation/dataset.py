"""Read-only validation for the Phase 1 evaluation dataset contract."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


SUPPORTED_SCHEMA_VERSION = "1.0"
ALLOWED_SPLITS = frozenset({"smoke", "development", "validation", "test"})
ALLOWED_DATASET_ROLES = frozenset({"target", "robustness", "public"})
IMAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
EXPECTED_FIELDS = (
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
STRING_EXPECTED_FIELDS = frozenset(
    {
        "customer_part_number",
        "so_number",
        "our_part_number",
        "carton_number",
        "datamatrix",
    }
)
NUMERIC_EXPECTED_FIELDS = frozenset({"quantity", "net_weight", "gross_weight"})
BUSINESS_STATUSES = frozenset({"PASS", "REVIEW", "FAIL", "ERROR"})
MANIFEST_COLUMNS = (
    "image_id",
    "inspection_group_id",
    "image_path",
    "split",
    "human_verified",
    "source",
    "synthetic",
    "parent_image_id",
)
LIGHTING_VALUES = frozenset(
    {"unknown", "normal", "dark", "bright", "screen_capture", "mixed"}
)
ROTATION_VALUES = frozenset({"unknown", "0-10", "10-20", "20-45", ">45"})
DISTANCE_VALUES = frozenset({"unknown", "near", "normal", "far", "n/a"})
POSITION_VALUES = frozenset(
    {"unknown", "center", "edge", "left", "right", "top", "bottom", "varied", "n/a"}
)
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"})


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    path: Optional[str] = None
    image_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.image_id is not None:
            payload["image_id"] = self.image_id
        return payload


@dataclass
class ValidationReport:
    dataset_path: str
    summary: dict[str, Any] = field(default_factory=dict)
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "ERROR"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "WARNING"]

    @property
    def infos(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "INFO"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dataset_path": self.dataset_path,
            "summary": self.summary,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "info": [issue.to_dict() for issue in self.infos],
        }


ImageReader = Callable[[Path], bool]


def validate_dataset(
    dataset: str | Path,
    *,
    image_reader: Optional[ImageReader] = None,
) -> ValidationReport:
    """Validate a Phase 1 dataset without changing any input file.

    ``image_reader`` is injectable for unit tests. The CLI uses the default
    OpenCV reader, which verifies that each image can be decoded.
    """

    root = Path(dataset).expanduser().resolve()
    report = ValidationReport(dataset_path=str(root))
    if not root.exists() or not root.is_dir():
        _add(report, "ERROR", "DATASET_MISSING", f"Dataset directory not found: {root}")
        report.summary = _empty_summary()
        return report

    config = _load_json(root / "dataset_config.json", report)
    ground_truth = _load_json(root / "ground_truth.json", report)
    manifest = _load_manifest(root / "manifest.csv", report)
    if not isinstance(config, Mapping) or not isinstance(ground_truth, Mapping):
        report.summary = _empty_summary()
        return report

    opencv_version = "INJECTED_READER" if image_reader is not None else _opencv_version()

    _validate_schema_version(config.get("schema_version"), report, "dataset_config.json")
    _validate_schema_version(ground_truth.get("schema_version"), report, "ground_truth.json")
    dataset_id = config.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        _add(
            report,
            "ERROR",
            "INVALID_DATASET_ID",
            "dataset_config.dataset_id must be a non-empty string",
            path="dataset_config.json",
        )
    dataset_role = config.get("dataset_role")
    if dataset_role not in ALLOWED_DATASET_ROLES:
        _add(
            report,
            "ERROR",
            "INVALID_DATASET_ROLE",
            f"dataset_config.dataset_role must be one of: {', '.join(sorted(ALLOWED_DATASET_ROLES))}",
            path="dataset_config.json",
        )

    samples = ground_truth.get("images")
    if not isinstance(samples, list):
        _add(report, "ERROR", "INVALID_IMAGES_COLLECTION", "ground_truth.images must be a list")
        samples = []

    records: list[dict[str, Any]] = []
    seen_image_ids: set[str] = set()
    seen_image_paths: set[str] = set()
    group_splits: dict[str, set[str]] = {}
    verified_count = 0

    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            _add(report, "ERROR", "INVALID_SAMPLE_OBJECT", f"Sample {index} must be an object")
            continue
        record = dict(sample)
        image_id = record.get("image_id")
        image_path = record.get("image_path")
        group = record.get("inspection_group_id")
        split = record.get("split")
        human_verified = record.get("human_verified")
        sample_label = str(image_id) if image_id is not None else f"index:{index}"

        if not isinstance(image_id, str) or not image_id.strip():
            _add(report, "ERROR", "INVALID_IMAGE_ID", "image_id must be a non-empty string", image_id=sample_label)
        elif not is_valid_image_id(image_id):
            _add(
                report,
                "ERROR",
                "INVALID_IMAGE_ID_FORMAT",
                "image_id must match ^[A-Za-z0-9_-]{1,128}$",
                image_id=image_id,
            )
        elif image_id in seen_image_ids:
            _add(report, "ERROR", "DUPLICATE_IMAGE_ID", f"Duplicate image_id: {image_id}", image_id=image_id)
        else:
            seen_image_ids.add(image_id)

        if not isinstance(image_path, str) or not image_path.strip():
            _add(report, "ERROR", "INVALID_IMAGE_PATH", "image_path must be a non-empty string", image_id=sample_label)
        else:
            normalized_path = image_path.replace("\\", "/")
            if normalized_path in seen_image_paths:
                _add(report, "ERROR", "DUPLICATE_IMAGE_PATH", f"Duplicate image_path: {image_path}", image_id=sample_label)
            else:
                seen_image_paths.add(normalized_path)
            resolved_image = _safe_dataset_path(root, image_path)
            if resolved_image is None:
                _add(report, "ERROR", "IMAGE_PATH_OUTSIDE_DATASET", f"image_path escapes dataset: {image_path}", image_id=sample_label)
            elif not resolved_image.exists():
                _add(report, "ERROR", "IMAGE_MISSING", f"Image does not exist: {image_path}", path=image_path, image_id=sample_label)
            elif resolved_image.suffix.lower() not in IMAGE_EXTENSIONS:
                _add(report, "ERROR", "UNSUPPORTED_IMAGE_EXTENSION", f"Unsupported image extension: {image_path}", path=image_path, image_id=sample_label)
            else:
                reader = image_reader or _opencv_image_reader
                try:
                    readable = bool(reader(resolved_image))
                except RuntimeError as exc:
                    _add(report, "ERROR", "OPENCV_UNAVAILABLE", str(exc), path=image_path, image_id=sample_label)
                    readable = True
                except Exception as exc:
                    _add(report, "ERROR", "IMAGE_READ_ERROR", f"Could not read {image_path}: {exc}", path=image_path, image_id=sample_label)
                    readable = False
                if not readable:
                    _add(report, "ERROR", "IMAGE_UNREADABLE", f"OpenCV could not decode image: {image_path}", path=image_path, image_id=sample_label)

        if not isinstance(group, str) or not group.strip():
            _add(report, "ERROR", "MISSING_INSPECTION_GROUP_ID", "inspection_group_id must be a non-empty string", image_id=sample_label)

        if split not in ALLOWED_SPLITS:
            _add(report, "ERROR", "INVALID_SPLIT", f"Unsupported split: {split!r}", image_id=sample_label)
        elif isinstance(group, str):
            group_splits.setdefault(group, set()).add(split)

        if not isinstance(human_verified, bool):
            _add(report, "ERROR", "INVALID_HUMAN_VERIFIED", "human_verified must be boolean", image_id=sample_label)
        elif human_verified:
            verified_count += 1
        else:
            _add(report, "WARNING", "UNVERIFIED_SAMPLE", "human_verified=false; sample is excluded from production accuracy metrics", image_id=sample_label)

        _validate_sample_schema(record, report, sample_label)
        records.append(record)

    for group, splits in group_splits.items():
        if len(splits) > 1:
            _add(report, "ERROR", "GROUP_SPLIT_LEAKAGE", f"inspection_group_id {group} occurs in multiple splits: {sorted(splits)}")

    manifest_records = _validate_manifest(manifest, report)
    _compare_manifest_and_ground_truth(records, manifest_records, report)
    _validate_synthetic_parent_splits(manifest_records, report)
    if dataset_role == "target":
        for image_id, row in manifest_records.items():
            if row.get("synthetic") is True:
                _add(
                    report,
                    "ERROR",
                    "SYNTHETIC_IN_TARGET_DATASET",
                    "Synthetic samples must be stored in a separate robustness dataset",
                    path="manifest.csv",
                    image_id=image_id,
                )

    split_distribution: dict[str, int] = {}
    synthetic_count = 0
    real_count = 0
    for row in manifest_records.values():
        split = row.get("split")
        if split in ALLOWED_SPLITS:
            split_distribution[split] = split_distribution.get(split, 0) + 1
        if row.get("synthetic") is True:
            synthetic_count += 1
        else:
            real_count += 1

    report.summary = {
        "opencv_version": opencv_version,
        "dataset_id": dataset_id,
        "dataset_role": dataset_role,
        "dataset_samples": len(records),
        "real_samples": real_count,
        "synthetic_samples": synthetic_count,
        "human_verified_samples": verified_count,
        "unverified_samples": max(0, len(records) - verified_count),
        "production_verified_samples": (
            sum(
                1
                for row in manifest_records.values()
                if row.get("human_verified") is True
                and row.get("synthetic") is not True
            )
            if dataset_role == "target"
            else 0
        ),
        "split_distribution": split_distribution,
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "info_count": len(report.infos),
    }
    return report


def _empty_summary() -> dict[str, Any]:
    return {
        "opencv_version": None,
        "dataset_id": None,
        "dataset_role": None,
        "dataset_samples": 0,
        "real_samples": 0,
        "synthetic_samples": 0,
        "human_verified_samples": 0,
        "unverified_samples": 0,
        "production_verified_samples": 0,
        "split_distribution": {},
    }


def _add(
    report: ValidationReport,
    severity: str,
    code: str,
    message: str,
    *,
    path: Optional[str] = None,
    image_id: Optional[str] = None,
) -> None:
    report.issues.append(ValidationIssue(severity, code, message, path, image_id))


def _load_json(path: Path, report: ValidationReport) -> Any:
    if not path.exists():
        _add(report, "ERROR", "REQUIRED_FILE_MISSING", f"Required file missing: {path.name}", path=path.name)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _add(report, "ERROR", "MALFORMED_JSON", f"Could not parse {path.name}: {exc}", path=path.name)
        return None


def _load_manifest(path: Path, report: ValidationReport) -> list[dict[str, str]]:
    if not path.exists():
        _add(report, "ERROR", "REQUIRED_FILE_MISSING", "Required file missing: manifest.csv", path="manifest.csv")
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or any(column not in reader.fieldnames for column in MANIFEST_COLUMNS):
                _add(report, "ERROR", "INVALID_MANIFEST_COLUMNS", f"manifest.csv must contain columns: {', '.join(MANIFEST_COLUMNS)}", path="manifest.csv")
                return []
            return [dict(row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        _add(report, "ERROR", "MALFORMED_MANIFEST", f"Could not parse manifest.csv: {exc}", path="manifest.csv")
        return []


def _validate_schema_version(value: Any, report: ValidationReport, path: str) -> None:
    if value is None:
        _add(report, "ERROR", "MISSING_SCHEMA_VERSION", f"{path} has no schema_version", path=path)
    elif value != SUPPORTED_SCHEMA_VERSION:
        _add(report, "ERROR", "UNSUPPORTED_SCHEMA_VERSION", f"{path} schema_version {value!r} is not supported", path=path)


def _safe_dataset_path(root: Path, relative_path: str) -> Optional[Path]:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _validate_sample_schema(sample: Mapping[str, Any], report: ValidationReport, image_id: str) -> None:
    if sample.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        _add(report, "ERROR", "UNSUPPORTED_SCHEMA_VERSION", "sample schema_version is unsupported or missing", image_id=image_id)

    expected = sample.get("expected")
    if not isinstance(expected, Mapping):
        _add(report, "ERROR", "INVALID_EXPECTED_OBJECT", "expected must be an object", image_id=image_id)
        expected = {}
    for field_name in EXPECTED_FIELDS:
        if field_name not in expected:
            _add(report, "ERROR", "MISSING_EXPECTED_FIELD", f"expected.{field_name} is missing", image_id=image_id)
            continue
        value = expected[field_name]
        if value is not None and field_name in STRING_EXPECTED_FIELDS and not isinstance(value, str):
            _add(report, "ERROR", "INVALID_EXPECTED_TYPE", f"expected.{field_name} must be string or null", image_id=image_id)
        if value is not None and field_name in NUMERIC_EXPECTED_FIELDS and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
        ):
            _add(report, "ERROR", "INVALID_EXPECTED_TYPE", f"expected.{field_name} must be finite number or null", image_id=image_id)
        if field_name == "expected_business_status" and value is not None and value not in BUSINESS_STATUSES:
            _add(report, "ERROR", "INVALID_BUSINESS_STATUS", f"Unsupported expected business status: {value!r}", image_id=image_id)

    conditions = sample.get("conditions")
    if not isinstance(conditions, Mapping):
        _add(report, "ERROR", "INVALID_CONDITIONS_OBJECT", "conditions must be an object", image_id=image_id)
        return
    _validate_enum(conditions, "lighting", LIGHTING_VALUES, report, image_id)
    _validate_enum(conditions, "rotation_bucket", ROTATION_VALUES, report, image_id)
    _validate_enum(conditions, "distance_bucket", DISTANCE_VALUES, report, image_id)
    _validate_enum(conditions, "position_bucket", POSITION_VALUES, report, image_id)
    for field_name in ("glare", "blur", "occluded", "negative_sample"):
        value = conditions.get(field_name)
        if not isinstance(value, bool) and not (value is None and field_name != "negative_sample"):
            _add(report, "ERROR", "INVALID_CONDITION_TYPE", f"conditions.{field_name} must be boolean or null", image_id=image_id)

def _validate_enum(
    conditions: Mapping[str, Any],
    field_name: str,
    allowed: Iterable[str],
    report: ValidationReport,
    image_id: str,
) -> None:
    value = conditions.get(field_name)
    if not isinstance(value, str) or value not in allowed:
        _add(report, "ERROR", "INVALID_CONDITION_ENUM", f"Unsupported conditions.{field_name}: {value!r}", image_id=image_id)


def _validate_manifest(rows: list[dict[str, str]], report: ValidationReport) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        row_number = index + 2
        image_id = raw.get("image_id", "")
        if not image_id:
            _add(report, "ERROR", "MANIFEST_IMAGE_ID_MISSING", f"manifest row {row_number} has no image_id", path="manifest.csv")
            continue
        if image_id in result:
            _add(report, "ERROR", "DUPLICATE_MANIFEST_IMAGE_ID", f"Duplicate manifest image_id: {image_id}", path="manifest.csv", image_id=image_id)
            continue
        split = raw.get("split", "")
        if split not in ALLOWED_SPLITS:
            _add(report, "ERROR", "INVALID_SPLIT", f"Unsupported manifest split: {split!r}", path="manifest.csv", image_id=image_id)
        human_verified = _parse_bool(raw.get("human_verified"))
        synthetic = _parse_bool(raw.get("synthetic"))
        if human_verified is None:
            _add(report, "ERROR", "INVALID_MANIFEST_BOOLEAN", "manifest human_verified must be true/false", path="manifest.csv", image_id=image_id)
        if synthetic is None:
            _add(report, "ERROR", "INVALID_MANIFEST_BOOLEAN", "manifest synthetic must be true/false", path="manifest.csv", image_id=image_id)
        result[image_id] = {
            **raw,
            "human_verified": human_verified,
            "synthetic": synthetic,
            "parent_image_id": raw.get("parent_image_id") or None,
        }
    return result


def _compare_manifest_and_ground_truth(
    samples: list[dict[str, Any]],
    manifest: dict[str, dict[str, Any]],
    report: ValidationReport,
) -> None:
    sample_by_id = {sample.get("image_id"): sample for sample in samples if sample.get("image_id")}
    for image_id, sample in sample_by_id.items():
        row = manifest.get(image_id)
        if row is None:
            _add(report, "ERROR", "MANIFEST_ROW_MISSING", f"No manifest row for {image_id}", image_id=image_id)
            continue
        for field_name in ("inspection_group_id", "image_path", "split"):
            if row.get(field_name) != sample.get(field_name):
                _add(report, "ERROR", "MANIFEST_MISMATCH", f"Manifest {field_name} does not match ground truth", image_id=image_id)
        if row.get("human_verified") != sample.get("human_verified"):
            _add(report, "ERROR", "MANIFEST_MISMATCH", "Manifest human_verified does not match ground truth", image_id=image_id)
    for image_id in manifest:
        if image_id not in sample_by_id:
            _add(report, "ERROR", "MANIFEST_ORPHAN", f"Manifest row has no ground-truth sample: {image_id}", path="manifest.csv", image_id=image_id)


def _validate_synthetic_parent_splits(manifest: dict[str, dict[str, Any]], report: ValidationReport) -> None:
    for image_id, row in manifest.items():
        if row.get("synthetic") is not True:
            continue
        parent_id = row.get("parent_image_id")
        if not parent_id:
            _add(report, "ERROR", "SYNTHETIC_PARENT_MISSING", "Synthetic row must specify parent_image_id", path="manifest.csv", image_id=image_id)
            continue
        parent = manifest.get(parent_id)
        if parent is None:
            _add(report, "ERROR", "SYNTHETIC_PARENT_MISSING", f"Synthetic parent not found: {parent_id}", path="manifest.csv", image_id=image_id)
            continue
        if parent.get("split") != row.get("split"):
            _add(report, "ERROR", "SYNTHETIC_PARENT_SPLIT_MISMATCH", "Synthetic variant and parent must share split", path="manifest.csv", image_id=image_id)
        if parent.get("inspection_group_id") != row.get("inspection_group_id"):
            _add(report, "ERROR", "SYNTHETIC_PARENT_GROUP_MISMATCH", "Synthetic variant and parent must share inspection_group_id", path="manifest.csv", image_id=image_id)


def _parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def is_valid_image_id(value: Any) -> bool:
    """Return whether an image ID is safe for use as one path component."""

    return isinstance(value, str) and IMAGE_ID_PATTERN.fullmatch(value) is not None


def _opencv_image_reader(path: Path) -> bool:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("OpenCV is required to validate image readability") from exc
    image = cv2.imread(str(path))
    return image is not None and getattr(image, "size", 0) > 0


def _opencv_version() -> str | None:
    try:
        import cv2
    except ImportError:
        return None
    return str(getattr(cv2, "__version__", "unknown"))
