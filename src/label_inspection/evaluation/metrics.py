"""Ground-truth-aware metric helpers for Phase 1 evaluation.

The evaluator deliberately separates execution from measurement. A prediction
can be useful smoke evidence while still being ineligible for accuracy when
the dataset record is not human verified.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence


EVALUATION_FIELDS = (
    "customer_part_number",
    "so_number",
    "our_part_number",
    "quantity",
    "net_weight",
    "gross_weight",
    "carton_number",
    "datamatrix",
)
NUMERIC_FIELDS = {"quantity", "net_weight", "gross_weight"}


def exact_match(expected: Any, actual: Any) -> bool:
    """Compare a field after conservative type/format normalization."""

    if expected is None or actual is None:
        return expected is None and actual is None
    expected_number = _decimal(expected)
    actual_number = _decimal(actual)
    if expected_number is not None and actual_number is not None:
        return expected_number == actual_number
    expected_text = _canonical_text(expected)
    actual_text = _canonical_text(actual)
    return expected_text == actual_text


def calculate_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate exact-match skeleton metrics without inventing accuracy."""

    eligible = [
        record
        for record in records
        if record.get("ground_truth_verified") is True
        and record.get("included_in_accuracy_metrics") is True
    ]
    result: dict[str, Any] = {
        "accuracy_status": "VERIFIED" if eligible else "NOT_ENOUGH_VERIFIED_GROUND_TRUTH",
        "eligible_samples": len(eligible),
        "field_metrics": {},
        "business_status": _metric_for_field(eligible, "expected_business_status", _prediction_status),
    }
    for field_name in EVALUATION_FIELDS:
        result["field_metrics"][field_name] = _metric_for_field(
            eligible,
            field_name,
            lambda record, name=field_name: _prediction_field(record, name),
        )
    result["barcode"] = result["field_metrics"]["datamatrix"]
    return result


def calculate_phase1_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    dataset_role: str,
    required_fields: Sequence[str] = (),
    barcode_required: bool = False,
    semantic_blockers: Mapping[str, str] | None = None,
    min_condition_samples: int = 5,
) -> dict[str, Any]:
    """Build the complete Phase 1 metric model from evaluator records.

    Accuracy eligibility is intentionally stricter than runtime eligibility:
    only target-role, human-verified, non-synthetic records count. Runtime
    failures remain in the denominator so execution errors cannot inflate
    production accuracy.
    Quality, ROI, failure, and latency metrics still use unverified runtime
    records because they describe system behavior rather than ground truth.
    """

    if min_condition_samples < 1:
        raise ValueError("min_condition_samples must be at least 1")
    blockers = dict(semantic_blockers or {})
    eligible = _production_eligible_records(records, dataset_role)
    production_accuracy = "NOT_VERIFIED"
    if eligible:
        production_accuracy = (
            "VERIFIED_WITH_SEMANTIC_BLOCKERS"
            if blockers
            else "VERIFIED"
        )
    field_metrics = {}
    for field_name in EVALUATION_FIELDS:
        if field_name == "datamatrix":
            continue
        metric = _field_metric(eligible, field_name)
        if field_name in blockers:
            metric = {
                **metric,
                "accuracy": None,
                "metric": "NEEDS_BUSINESS_CONFIRMATION",
                "semantic_blocker": blockers[field_name],
            }
        field_metrics[field_name] = metric
    return {
        "production_accuracy": production_accuracy,
        "eligible_samples": len(eligible),
        "total_records": len(records),
        "dataset_role": dataset_role,
        "semantic_blockers": blockers,
        "field_metrics": field_metrics,
        "barcode": _barcode_metrics(eligible),
        "business": _business_metrics(
            eligible,
            required_fields=required_fields,
            barcode_required=barcode_required,
            blocked_fields=frozenset(blockers),
        ),
        "quality": _quality_metrics(records),
        "roi": _roi_metrics(records),
        "latency": _latency_metrics(records),
        "condition_rows": _condition_metrics(
            records,
            dataset_role=dataset_role,
            min_condition_samples=min_condition_samples,
            required_fields=required_fields,
            barcode_required=barcode_required,
            blocked_fields=frozenset(blockers),
        ),
        "failure_rows": _failure_rows(
            records,
            required_fields=required_fields,
            barcode_required=barcode_required,
            blocked_fields=frozenset(blockers),
        ),
    }


def _production_eligible_records(
    records: Sequence[Mapping[str, Any]], dataset_role: str
) -> list[Mapping[str, Any]]:
    if dataset_role != "target":
        return []
    return [
        record
        for record in records
        if record.get("ground_truth_verified") is True
        and record.get("included_in_accuracy_metrics") is True
        and record.get("synthetic") is not True
    ]


def _field_metric(records: Sequence[Mapping[str, Any]], field_name: str) -> dict[str, Any]:
    if not records:
        return {
            "eligible_samples": 0,
            "exact_match": None,
            "accuracy": None,
            "missing_prediction": None,
            "wrong_prediction": None,
            "false_extraction": None,
            "metric": "NOT_VERIFIED",
        }
    exact = missing = wrong = false_extraction = 0
    for record in records:
        expected = record.get("expected", {}).get(field_name)
        actual = _prediction_field(record, field_name)
        if expected is None and actual is not None:
            false_extraction += 1
        elif expected is not None and actual is None:
            missing += 1
        elif _field_exact_match(field_name, expected, actual):
            exact += 1
        else:
            wrong += 1
    total = len(records)
    return {
        "eligible_samples": total,
        "exact_match": exact,
        "accuracy": exact / total,
        "missing_prediction": missing,
        "wrong_prediction": wrong,
        "false_extraction": false_extraction,
        "metric": exact / total,
    }


def _barcode_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "eligible_samples": 0,
            "expected_code_count": 0,
            "decode_success": None,
            "decode_success_rate": None,
            "exact_payload_match": None,
            "exact_payload_accuracy": None,
            "no_code": None,
            "wrong_payload": None,
            "wrong_format": None,
            "invalid_code": None,
            "multiple_code_case": None,
            "false_extraction": None,
            "metric": "NOT_VERIFIED",
        }
    expected_count = decode_success = exact = no_code = wrong = 0
    wrong_format = invalid = multiple = false_extraction = 0
    for record in records:
        expected = record.get("expected", {}).get("datamatrix")
        items = _barcode_items(record)
        datamatrix_items = [item for item in items if _is_datamatrix(item)]
        populated_datamatrix = [item for item in datamatrix_items if item.get("value") is not None]
        valid_datamatrix = [item for item in populated_datamatrix if item.get("valid") is True]
        populated_items = [item for item in items if item.get("value") is not None]
        if len(populated_items) > 1:
            multiple += 1
        if any(item.get("valid") is False for item in populated_datamatrix):
            invalid += 1
        if expected is None:
            if populated_datamatrix:
                false_extraction += 1
            continue
        expected_count += 1
        if populated_datamatrix:
            decode_success += 1
        else:
            no_code += 1
        if any(str(item.get("value")) == str(expected) for item in valid_datamatrix):
            exact += 1
        elif valid_datamatrix:
            wrong += 1
        if not populated_datamatrix and any(not _is_datamatrix(item) for item in populated_items):
            wrong_format += 1
    metric: float | str
    if expected_count:
        metric = exact / expected_count
    else:
        metric = "NOT_APPLICABLE"
    return {
        "eligible_samples": len(records),
        "expected_code_count": expected_count,
        "decode_success": decode_success,
        "decode_success_rate": decode_success / expected_count if expected_count else None,
        "exact_payload_match": exact,
        "exact_payload_accuracy": exact / expected_count if expected_count else None,
        "no_code": no_code,
        "wrong_payload": wrong,
        "wrong_format": wrong_format,
        "invalid_code": invalid,
        "multiple_code_case": multiple,
        "false_extraction": false_extraction,
        "metric": metric,
    }


def _business_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    required_fields: Sequence[str],
    barcode_required: bool,
    blocked_fields: frozenset[str],
) -> dict[str, Any]:
    if not records:
        return {
            "eligible_samples": 0,
            "status_counts": None,
            "false_pass": None,
            "unnecessary_review": None,
            "metric": "NOT_VERIFIED",
        }
    statuses = Counter(_prediction_status(record) or "ERROR" for record in records)
    expected_statuses = [record.get("expected", {}).get("expected_business_status") for record in records]
    false_pass = sum(
        _prediction_status(record) == "PASS"
        and (
            (expected is not None and expected != "PASS")
            or not _required_outputs_match(
                record,
                required_fields=required_fields,
                barcode_required=barcode_required,
                blocked_fields=blocked_fields,
            )
        )
        for record, expected in zip(records, expected_statuses)
    )
    unnecessary_review = sum(
        expected == "PASS" and _prediction_status(record) == "REVIEW"
        for record, expected in zip(records, expected_statuses)
    )
    return {
        "eligible_samples": len(records),
        "status_counts": {status: statuses.get(status, 0) for status in ("PASS", "REVIEW", "FAIL", "ERROR")},
        "false_pass": false_pass,
        "unnecessary_review": unnecessary_review,
        "metric": "VERIFIED",
    }


def _required_outputs_match(
    record: Mapping[str, Any],
    *,
    required_fields: Sequence[str],
    barcode_required: bool,
    blocked_fields: frozenset[str],
) -> bool:
    expected = record.get("expected") or {}
    for field_name in required_fields:
        if field_name in blocked_fields:
            continue
        if not _field_exact_match(
            field_name,
            expected.get(field_name),
            _prediction_field(record, field_name),
        ):
            return False
    if barcode_required and not _expected_datamatrix_matches(record):
        return False
    return True


def _expected_datamatrix_matches(record: Mapping[str, Any]) -> bool:
    expected = (record.get("expected") or {}).get("datamatrix")
    valid_datamatrix = [
        item
        for item in _barcode_items(record)
        if _is_datamatrix(item)
        and item.get("valid") is True
        and item.get("value") is not None
    ]
    if expected is None:
        return not valid_datamatrix
    return any(str(item.get("value")) == str(expected) for item in valid_datamatrix)


def _quality_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    potential_false_reject = 0
    for record in records:
        quality = (record.get("prediction") or {}).get("quality") or {}
        status = quality.get("status") or "NOT_RUN"
        counts[status] += 1
        if status in {"FAIL", "REJECT", "ERROR"} and _has_observation_success(record):
            potential_false_reject += 1
    total = len(records)
    return {
        "sample_count": total,
        "quality_pass": counts.get("PASS", 0),
        "quality_reject": counts.get("FAIL", 0) + counts.get("REJECT", 0),
        "quality_error": counts.get("ERROR", 0),
        "quality_not_run": counts.get("NOT_RUN", 0),
        "potential_false_reject": potential_false_reject,
        "reject_rate": (counts.get("FAIL", 0) + counts.get("REJECT", 0)) / total if total else None,
    }


def _roi_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    success = 0
    failures = 0
    for record in records:
        roi = (record.get("prediction") or {}).get("roi") or {}
        if roi.get("label_bbox") and roi.get("crop_bbox"):
            success += 1
        if record.get("failure_stage") == "ROI":
            failures += 1
    total = len(records)
    return {
        "sample_count": total,
        "roi_success": success,
        "roi_failure": failures,
        "crop_success": success,
        "success_rate": success / total if total else None,
    }


def _latency_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stages = ("ocr_ms", "barcode_ms", "preprocessing_ms", "total_pipeline_ms")
    return {stage: _summary_stats(_timing_values(records, stage)) for stage in stages}


def _summary_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None, "metric": "NOT_AVAILABLE"}
    ordered = sorted(float(value) for value in values)
    return {
        "count": len(ordered),
        "mean": sum(ordered) / len(ordered),
        "p50": _nearest_rank(ordered, 0.50),
        "p95": _nearest_rank(ordered, 0.95),
        "p99": _nearest_rank(ordered, 0.99),
        "max": ordered[-1],
        "metric": "AVAILABLE",
    }


def _nearest_rank(values: Sequence[float], fraction: float) -> float:
    index = max(0, min(len(values) - 1, int(len(values) * fraction + 0.999999) - 1))
    return values[index]


def _condition_metrics(
    records: Sequence[Mapping[str, Any]],
    *,
    dataset_role: str,
    min_condition_samples: int,
    required_fields: Sequence[str],
    barcode_required: bool,
    blocked_fields: frozenset[str],
) -> list[dict[str, Any]]:
    dimensions = {
        "lighting": "lighting",
        "glare": "glare",
        "blur": "blur",
        "occlusion": "occluded",
        "rotation": "rotation_bucket",
        "distance": "distance_bucket",
        "position": "position_bucket",
    }
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("synthetic") is True:
            continue
        conditions = record.get("conditions") or {}
        for dimension, source_key in dimensions.items():
            value = conditions.get(source_key)
            if value is None:
                continue
            groups[(dimension, str(value))].append(record)
    rows = []
    for (condition, value), group in sorted(groups.items()):
        quality = _quality_metrics(group)
        eligible = _production_eligible_records(group, dataset_role)
        exact_records = sum(
            _required_outputs_match(
                record,
                required_fields=required_fields,
                barcode_required=barcode_required,
                blocked_fields=blocked_fields,
            )
            for record in eligible
        )
        business = _business_metrics(
            eligible,
            required_fields=required_fields,
            barcode_required=barcode_required,
            blocked_fields=blocked_fields,
        )
        insufficient = len(eligible) < min_condition_samples
        if not eligible:
            accuracy_status = "NOT_VERIFIED"
        elif insufficient:
            accuracy_status = "INSUFFICIENT_SAMPLE_SIZE"
        else:
            accuracy_status = "VERIFIED"
        rows.append({
            "condition": condition,
            "value": value,
            "sample_count": len(group),
            "eligible_samples": len(eligible),
            "accuracy_status": accuracy_status,
            "min_condition_samples": min_condition_samples,
            "insufficient_sample_size": insufficient,
            "accuracy": exact_records / len(eligible) if eligible else None,
            "review_rate": (
                sum(_prediction_status(record) == "REVIEW" for record in group) / len(group)
                if group else None
            ),
            "false_pass": business["false_pass"],
            "latency": _latency_metrics(group)["total_pipeline_ms"],
            "quality_reject_rate": quality["reject_rate"],
            "roi_success_rate": _roi_metrics(group)["success_rate"],
            "ocr_success": sum(bool(((r.get("prediction") or {}).get("observation_result") or {}).get("ocr", {}).get("success") or (r.get("prediction") or {}).get("errors", {}).get("ocr") is None and (r.get("prediction") or {}).get("timings", {}).get("ocr_ms", 0) > 0) for r in group),
            "barcode_decode_success": sum(bool(_prediction_field(r, "datamatrix")) for r in group),
            "failure_count": sum(bool(r.get("failure_stage")) for r in group),
        })
    return rows


def _failure_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    required_fields: Sequence[str],
    barcode_required: bool,
    blocked_fields: frozenset[str],
) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        stage = record.get("failure_stage")
        prediction = record.get("prediction") or {}
        status = _prediction_status(record)
        outputs_match = _required_outputs_match(
            record,
            required_fields=required_fields,
            barcode_required=barcode_required,
            blocked_fields=blocked_fields,
        )
        if not stage and status == "PASS" and outputs_match:
            continue
        error = prediction.get("errors") or record.get("failure") or {}
        fields = prediction.get("fields") or {}
        predicted_fields = {
            name: value.get("value") if isinstance(value, Mapping) else value
            for name, value in fields.items()
        }
        confidences = {
            name: value.get("confidence")
            for name, value in fields.items()
            if isinstance(value, Mapping)
        }
        rows.append({
            "image_id": record.get("image_id"),
            "failure_stage": stage,
            "failure_reason": stage or ("GROUND_TRUTH_MISMATCH" if not outputs_match else "BUSINESS_STATUS"),
            "error_type": error.get("error_type") if isinstance(error, Mapping) else None,
            "error_message": error.get("error_message") if isinstance(error, Mapping) else str(error),
            "status": status,
            "expected": dict(record.get("expected") or {}),
            "predicted_fields": predicted_fields,
            "field_confidences": confidences,
            "ocr": prediction.get("ocr") or (prediction.get("observation_result") or {}).get("ocr"),
            "barcode": prediction.get("barcode"),
            "quality": prediction.get("quality"),
            "roi": prediction.get("roi"),
            "validator": prediction.get("production_decision"),
            "artifacts": record.get("artifacts") or prediction.get("artifacts"),
        })
    return rows


def _metric_for_field(
    records: Sequence[Mapping[str, Any]],
    expected_name: str,
    actual_getter,
) -> dict[str, Any]:
    if not records:
        return {
            "eligible_samples": 0,
            "exact_matches": None,
            "metric": "NOT_VERIFIED",
            "accuracy": None,
        }
    exact_matches = sum(
        exact_match(record.get("expected", {}).get(expected_name), actual_getter(record))
        for record in records
    )
    accuracy = exact_matches / len(records)
    return {
        "eligible_samples": len(records),
        "exact_matches": exact_matches,
        "metric": accuracy,
        "accuracy": accuracy,
    }


def _prediction_field(record: Mapping[str, Any], field_name: str) -> Any:
    if field_name == "datamatrix":
        prediction = record.get("prediction") or {}
        barcode = prediction.get("barcode") or {}
        selected = barcode.get("selected") or {}
        return selected.get("value")
    prediction = record.get("prediction") or {}
    fields = prediction.get("fields") or {}
    field = fields.get(field_name)
    if isinstance(field, Mapping):
        return field.get("value")
    return field


def _field_exact_match(field_name: str, expected: Any, actual: Any) -> bool:
    if field_name == "quantity":
        expected_number = _decimal(expected)
        actual_number = _decimal(actual)
        return expected_number is not None and actual_number is not None and expected_number == actual_number
    if field_name in {"net_weight", "gross_weight"}:
        expected_weight = _weight_kg(expected)
        actual_weight = _weight_kg(actual)
        return expected_weight is not None and actual_weight is not None and expected_weight == actual_weight
    if field_name == "datamatrix":
        if expected is None or actual is None:
            return expected is None and actual is None
        return str(expected) == str(actual)
    if expected is None or actual is None:
        return expected is None and actual is None
    return _canonical_text(expected) == _canonical_text(actual)


def _selected_barcode(record: Mapping[str, Any]) -> Mapping[str, Any]:
    prediction = record.get("prediction") or {}
    barcode = prediction.get("barcode") or {}
    selected = barcode.get("selected") or {}
    return selected if isinstance(selected, Mapping) else {}


def _barcode_items(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    prediction = record.get("prediction") or {}
    barcode = prediction.get("barcode") or {}
    raw_items = barcode.get("items") or []
    items = [item for item in raw_items if isinstance(item, Mapping)]
    selected = _selected_barcode(record)
    if selected and not items:
        items.append(selected)
    return items


def _is_datamatrix(item: Mapping[str, Any]) -> bool:
    normalized = re.sub(r"[^A-Z0-9]", "", str(item.get("format") or "").upper())
    return normalized == "DATAMATRIX"


def _has_observation_success(record: Mapping[str, Any]) -> bool:
    observation = (record.get("prediction") or {}).get("observation_result") or {}
    ocr = observation.get("ocr") or {}
    barcode = observation.get("barcode") or {}
    selected = barcode.get("selected") or {}
    return bool(ocr.get("success") or selected.get("success"))


def _timing_values(records: Sequence[Mapping[str, Any]], stage: str) -> list[float]:
    values = []
    for record in records:
        timings = (record.get("prediction") or {}).get("timings") or {}
        value = timings.get(stage)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _prediction_status(record: Mapping[str, Any]) -> Any:
    prediction = record.get("prediction") or {}
    return prediction.get("business_status") or prediction.get("status")


def _canonical_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).upper()


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return decimal if decimal.is_finite() else None
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if not candidate or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", candidate):
            return None
        try:
            decimal = Decimal(candidate)
        except InvalidOperation:
            return None
        return decimal if decimal.is_finite() else None
    return None


def _weight_kg(value: Any) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return _decimal(value)
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]*)\s*",
        value.replace(",", ""),
    )
    if not match:
        return None
    number = _decimal(match.group(1))
    if number is None:
        return None
    unit = match.group(2).upper()
    if unit in {"", "KG", "KGS"}:
        return number
    if unit == "G":
        return number / Decimal("1000")
    if unit in {"LB", "LBS"}:
        return number * Decimal("0.45359237")
    return None
