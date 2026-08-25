from label_inspection.evaluation.metrics import (
    calculate_metrics,
    calculate_phase1_metrics,
    exact_match,
)


def _record(*, verified: bool, actual: str | None):
    return {
        "ground_truth_verified": verified,
        "included_in_accuracy_metrics": verified,
        "expected": {
            "customer_part_number": "CP-01",
            "expected_business_status": "PASS",
        },
        "prediction": {
            "business_status": "PASS",
            "fields": {"customer_part_number": {"value": actual}},
        },
    }


def test_exact_match_normalizes_numeric_and_whitespace_values():
    assert exact_match("  cp-01 ", "CP-01")
    assert exact_match(5.24, "5.240")
    assert not exact_match("CP-01", "CP-02")


def test_unverified_samples_are_excluded_without_zero_percent_claim():
    metrics = calculate_metrics([_record(verified=False, actual="WRONG")])
    assert metrics["accuracy_status"] == "NOT_ENOUGH_VERIFIED_GROUND_TRUTH"
    assert metrics["eligible_samples"] == 0
    assert metrics["field_metrics"]["customer_part_number"]["metric"] == "NOT_VERIFIED"
    assert metrics["field_metrics"]["customer_part_number"]["accuracy"] is None


def test_verified_sample_is_included_in_exact_match_metric():
    metrics = calculate_metrics([_record(verified=True, actual="CP-01")])
    assert metrics["accuracy_status"] == "VERIFIED"
    assert metrics["eligible_samples"] == 1
    assert metrics["field_metrics"]["customer_part_number"]["exact_matches"] == 1
    assert metrics["field_metrics"]["customer_part_number"]["metric"] == 1.0


def _phase_record(
    image_id,
    *,
    expected_customer,
    predicted_customer,
    expected_status="PASS",
    predicted_status="PASS",
    synthetic=False,
    quality_status="PASS",
    failure_stage=None,
    conditions=None,
    included=True,
    barcode_items=None,
    barcode_format="DataMatrix",
    barcode_valid=True,
):
    return {
        "image_id": image_id,
        "ground_truth_verified": True,
        "included_in_accuracy_metrics": included,
        "synthetic": synthetic,
        "expected": {
            "customer_part_number": expected_customer,
            "datamatrix": "DM-01" if expected_customer else None,
            "expected_business_status": expected_status,
        },
        "prediction": {
            "status": predicted_status,
            "business_status": predicted_status,
            "fields": {"customer_part_number": {"value": predicted_customer}},
            "barcode": {
                "selected": {
                    "value": "DM-01" if predicted_customer else None,
                    "success": bool(predicted_customer),
                    "format": barcode_format if predicted_customer else None,
                    "valid": barcode_valid if predicted_customer else None,
                },
                "items": barcode_items or [],
            },
            "quality": {"status": quality_status},
            "timings": {"ocr_ms": 10.0, "barcode_ms": 4.0, "preprocessing_ms": 2.0, "total_pipeline_ms": 20.0},
            "observation_result": None,
        },
        "conditions": conditions or {"synthetic_condition": "normal"},
        "failure_stage": failure_stage,
    }


def _phase_metrics(records, **overrides):
    options = {
        "dataset_role": "target",
        "required_fields": ("customer_part_number",),
        "barcode_required": True,
    }
    options.update(overrides)
    return calculate_phase1_metrics(records, **options)


def test_phase1_field_metrics_classify_exact_missing_wrong_and_false_extraction():
    records = [
        _phase_record("EXACT", expected_customer="CP-01", predicted_customer="CP-01"),
        _phase_record("MISSING", expected_customer="CP-02", predicted_customer=None),
        _phase_record("WRONG", expected_customer="CP-03", predicted_customer="CP-XX"),
        _phase_record("FALSE", expected_customer=None, predicted_customer="CP-04", expected_status="REVIEW", predicted_status="REVIEW"),
    ]
    metrics = _phase_metrics(records)
    field = metrics["field_metrics"]["customer_part_number"]
    assert field["eligible_samples"] == 4
    assert field["exact_match"] == 1
    assert field["missing_prediction"] == 1
    assert field["wrong_prediction"] == 1
    assert field["false_extraction"] == 1
    assert field["accuracy"] == 0.25


def test_phase1_metrics_exclude_synthetic_from_production_accuracy():
    records = [
        _phase_record("REAL", expected_customer="CP-01", predicted_customer="CP-01"),
        _phase_record("SYNTH", expected_customer="CP-01", predicted_customer="WRONG", synthetic=True),
    ]
    metrics = _phase_metrics(records)
    assert metrics["eligible_samples"] == 1
    assert metrics["field_metrics"]["customer_part_number"]["accuracy"] == 1.0


def test_phase1_metrics_report_latency_percentiles_and_business_rates():
    records = [
        _phase_record("PASS", expected_customer="CP-01", predicted_customer="CP-01", predicted_status="PASS"),
        _phase_record("REVIEW", expected_customer="CP-02", predicted_customer="CP-XX", expected_status="PASS", predicted_status="REVIEW"),
    ]
    records[0]["prediction"]["timings"]["total_pipeline_ms"] = 10.0
    records[1]["prediction"]["timings"]["total_pipeline_ms"] = 30.0
    metrics = _phase_metrics(records)
    assert metrics["latency"]["total_pipeline_ms"]["mean"] == 20.0
    assert metrics["latency"]["total_pipeline_ms"]["p50"] == 10.0
    assert metrics["business"]["status_counts"]["PASS"] == 1
    assert metrics["business"]["unnecessary_review"] == 1


def test_phase1_metrics_have_not_verified_status_when_no_eligible_samples():
    metrics = _phase_metrics([_phase_record("UNVERIFIED", expected_customer="CP-01", predicted_customer="CP-01") | {"ground_truth_verified": False, "included_in_accuracy_metrics": False}])
    assert metrics["production_accuracy"] == "NOT_VERIFIED"
    assert metrics["eligible_samples"] == 0
    assert metrics["field_metrics"]["customer_part_number"]["metric"] == "NOT_VERIFIED"


def test_verified_runtime_failure_remains_in_accuracy_denominator():
    records = [
        _phase_record("GOOD", expected_customer="CP-01", predicted_customer="CP-01"),
        _phase_record(
            "OCR_FAILURE",
            expected_customer="CP-02",
            predicted_customer=None,
            predicted_status="ERROR",
            failure_stage="OCR",
            included=True,
        ),
    ]

    metrics = _phase_metrics(records)

    assert metrics["eligible_samples"] == 2
    field = metrics["field_metrics"]["customer_part_number"]
    assert field["accuracy"] == 0.5
    assert field["missing_prediction"] == 1


def test_false_pass_counts_wrong_or_missing_required_verified_data():
    wrong = _phase_record(
        "WRONG_PASS",
        expected_customer="CP-01",
        predicted_customer="WRONG",
        expected_status="PASS",
        predicted_status="PASS",
    )
    missing = _phase_record(
        "MISSING_PASS",
        expected_customer="CP-02",
        predicted_customer=None,
        expected_status="PASS",
        predicted_status="PASS",
    )

    metrics = _phase_metrics([wrong, missing], barcode_required=False)

    assert metrics["business"]["false_pass"] == 2


def test_robustness_role_never_produces_production_accuracy():
    record = _phase_record("ROBUST", expected_customer="CP-01", predicted_customer="CP-01")

    metrics = _phase_metrics([record], dataset_role="robustness")

    assert metrics["production_accuracy"] == "NOT_VERIFIED"
    assert metrics["eligible_samples"] == 0


def test_semantically_blocked_field_is_not_reported_as_production_verified():
    record = _phase_record("BLOCKED", expected_customer="CP-01", predicted_customer="CP-01")

    metrics = _phase_metrics(
        [record],
        semantic_blockers={
            "customer_part_number": "KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION"
        },
    )

    field = metrics["field_metrics"]["customer_part_number"]
    assert field["accuracy"] is None
    assert field["metric"] == "NEEDS_BUSINESS_CONFIRMATION"
    assert metrics["production_accuracy"] == "VERIFIED_WITH_SEMANTIC_BLOCKERS"


def test_datamatrix_requires_exact_case_format_and_valid_flag():
    record = _phase_record("DM", expected_customer="CP-01", predicted_customer="CP-01")
    record["expected"]["datamatrix"] = "AbC-123"
    record["prediction"]["barcode"] = {
        "selected": {"value": "ABC-123", "format": "DataMatrix", "valid": True, "success": True},
        "items": [
            {"value": "ABC-123", "format": "DataMatrix", "valid": True, "success": True},
            {"value": "AbC-123", "format": "QRCode", "valid": True, "success": True},
            {"value": "AbC-123", "format": "DataMatrix", "valid": False, "success": True},
        ],
    }

    barcode = _phase_metrics([record])["barcode"]

    assert barcode["expected_code_count"] == 1
    assert barcode["exact_payload_match"] == 0
    assert barcode["wrong_payload"] == 1
    assert barcode["invalid_code"] == 1
    assert barcode["multiple_code_case"] == 1


def test_verified_negative_without_datamatrix_is_not_scored_zero_accuracy():
    record = _phase_record(
        "NO_CODE_EXPECTED",
        expected_customer=None,
        predicted_customer=None,
        expected_status="REVIEW",
        predicted_status="REVIEW",
    )

    barcode = _phase_metrics(
        [record], required_fields=(), barcode_required=False
    )["barcode"]

    assert barcode["expected_code_count"] == 0
    assert barcode["metric"] == "NOT_APPLICABLE"


def test_weight_with_kg_unit_matches_numeric_ground_truth():
    record = _phase_record("WEIGHT", expected_customer=None, predicted_customer=None)
    record["expected"]["net_weight"] = 12.5
    record["prediction"]["fields"] = {
        "net_weight": {"value": "12.5 KG", "confidence": 0.99}
    }

    metrics = _phase_metrics(
        [record], required_fields=("net_weight",), barcode_required=False
    )

    field = metrics["field_metrics"]["net_weight"]
    assert field["exact_match"] == 1
    assert field["accuracy"] == 1.0


def test_real_condition_metrics_cover_all_dimensions_and_operational_rates():
    conditions = {
        "lighting": "normal",
        "glare": False,
        "blur": False,
        "occluded": False,
        "rotation_bucket": "0-10",
        "distance_bucket": "near",
        "position_bucket": "center",
    }
    exact = _phase_record(
        "COND_EXACT",
        expected_customer="CP-01",
        predicted_customer="CP-01",
        conditions=conditions,
    )
    wrong_pass = _phase_record(
        "COND_FALSE_PASS",
        expected_customer="CP-02",
        predicted_customer="WRONG",
        conditions=conditions,
    )
    review = _phase_record(
        "COND_REVIEW",
        expected_customer="CP-03",
        predicted_customer=None,
        predicted_status="REVIEW",
        conditions=conditions,
    )
    synthetic = _phase_record(
        "COND_SYNTHETIC",
        expected_customer="CP-04",
        predicted_customer="CP-04",
        conditions=conditions,
        synthetic=True,
    )

    rows = _phase_metrics(
        [exact, wrong_pass, review, synthetic],
        barcode_required=False,
        min_condition_samples=3,
    )["condition_rows"]
    by_condition = {(row["condition"], str(row["value"]).lower()): row for row in rows}

    assert {row["condition"] for row in rows} == {
        "lighting", "glare", "blur", "occlusion", "rotation", "distance", "position"
    }
    lighting = by_condition[("lighting", "normal")]
    assert lighting["sample_count"] == 3
    assert lighting["eligible_samples"] == 3
    assert lighting["accuracy"] == 1 / 3
    assert lighting["review_rate"] == 1 / 3
    assert lighting["false_pass"] == 1
    assert lighting["latency"]["p50"] == 20.0
    assert lighting["insufficient_sample_size"] is False


def test_condition_sample_threshold_comes_from_evaluation_config():
    conditions = {"lighting": "normal"}
    records = [
        _phase_record("COND_1", expected_customer="CP-01", predicted_customer="CP-01", conditions=conditions),
        _phase_record("COND_2", expected_customer="CP-01", predicted_customer="CP-01", conditions=conditions),
    ]

    row = _phase_metrics(
        records,
        barcode_required=False,
        min_condition_samples=3,
    )["condition_rows"][0]

    assert row["min_condition_samples"] == 3
    assert row["insufficient_sample_size"] is True
    assert row["accuracy_status"] == "INSUFFICIENT_SAMPLE_SIZE"


def test_failure_rows_preserve_complete_trace_evidence():
    record = _phase_record(
        "FAIL_TRACE",
        expected_customer="CP-EXPECTED",
        predicted_customer="CP-PREDICTED",
        predicted_status="ERROR",
        failure_stage="OCR",
    )
    record["prediction"].update(
        {
            "fields": {
                "customer_part_number": {
                    "value": "CP-PREDICTED",
                    "confidence": 0.42,
                    "line_text": "Nvidia P/N: CP-PREDICTED",
                }
            },
            "ocr": {"lines": [{"text": "Nvidia P/N: CP-PREDICTED", "confidence": 0.42}]},
            "quality": {"status": "PASS", "glare_ratio": 0.1},
            "roi": {"label_bbox": [1, 2, 3, 4], "crop_bbox": [1, 2, 3, 4]},
            "production_decision": {"status": "ERROR", "reasons": ["OCR_FAILED"]},
            "errors": {"failure_stage": "OCR", "error_type": "RuntimeError", "error_message": "boom"},
        }
    )
    record["artifacts"] = {"directory": "/safe/run/samples/FAIL_TRACE"}

    row = _phase_metrics([record], barcode_required=False)["failure_rows"][0]

    assert row["expected"]["customer_part_number"] == "CP-EXPECTED"
    assert row["predicted_fields"]["customer_part_number"] == "CP-PREDICTED"
    assert row["field_confidences"]["customer_part_number"] == 0.42
    assert row["ocr"]["lines"][0]["text"].startswith("Nvidia P/N")
    assert row["barcode"]["selected"]["format"] == "DataMatrix"
    assert row["quality"]["status"] == "PASS"
    assert row["roi"]["crop_bbox"] == [1, 2, 3, 4]
    assert row["validator"]["reasons"] == ["OCR_FAILED"]
    assert row["artifacts"]["directory"].endswith("FAIL_TRACE")
