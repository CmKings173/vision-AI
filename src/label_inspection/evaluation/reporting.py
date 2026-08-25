"""Phase 1 report writers for JSON, CSV, and Markdown outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def write_phase1_outputs(
    output: str | Path,
    *,
    summary: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> dict[str, str]:
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    report_summary = {
        **dict(summary),
        "production_accuracy": metrics.get("production_accuracy"),
        "eligible_samples": metrics.get("eligible_samples", 0),
        "metrics": dict(metrics),
    }
    paths = {
        "summary": root / "summary.json",
        "field_metrics": root / "field_metrics.csv",
        "barcode_metrics": root / "barcode_metrics.json",
        "business_metrics": root / "business_metrics.json",
        "roi_metrics": root / "roi_metrics.json",
        "quality_metrics": root / "quality_metrics.json",
        "condition_metrics": root / "condition_metrics.csv",
        "latency_metrics": root / "latency_metrics.json",
        "failures": root / "failures.csv",
        "evaluation_report": root / "evaluation_report.md",
    }
    _write_json(paths["summary"], report_summary)
    _write_json(paths["barcode_metrics"], metrics.get("barcode", {}))
    _write_json(paths["business_metrics"], metrics.get("business", {}))
    _write_json(paths["roi_metrics"], metrics.get("roi", {}))
    _write_json(paths["quality_metrics"], metrics.get("quality", {}))
    _write_json(paths["latency_metrics"], metrics.get("latency", {}))
    _write_field_csv(paths["field_metrics"], metrics.get("field_metrics", {}))
    _write_csv(paths["condition_metrics"], metrics.get("condition_rows", []), [
        "condition", "value", "sample_count", "eligible_samples", "accuracy_status",
        "min_condition_samples", "insufficient_sample_size", "accuracy",
        "review_rate", "false_pass", "latency", "quality_reject_rate",
        "roi_success_rate", "ocr_success", "barcode_decode_success",
        "failure_count",
    ])
    _write_csv(paths["failures"], metrics.get("failure_rows", []), [
        "image_id", "failure_stage", "failure_reason", "error_type",
        "error_message", "status", "expected", "predicted_fields",
        "field_confidences", "ocr", "barcode", "quality", "roi",
        "validator", "artifacts",
    ])
    paths["evaluation_report"].write_text(
        _render_report(report_summary, metrics), encoding="utf-8"
    )
    return {key: str(path) for key, path in paths.items()}


def _write_field_csv(path: Path, fields: Mapping[str, Mapping[str, Any]]) -> None:
    fieldnames = [
        "field", "eligible_samples", "exact_match", "accuracy",
        "missing_prediction", "wrong_prediction", "false_extraction", "metric",
        "semantic_blocker",
    ]
    rows = [{"field": field_name, **dict(values)} for field_name, values in fields.items()]
    _write_csv(path, rows, fieldnames)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            normalized = {
                key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (Mapping, list, tuple))
                else value
                for key, value in row.items()
            }
            writer.writerow(normalized)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_report(summary: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    startup = summary.get("startup", {})
    quality = metrics.get("quality", {})
    lines = [
        "# Phase 1 evaluation report",
        "",
        f"- Status: `{summary.get('status')}`",
        f"- Dataset: `{summary.get('dataset')}`",
        f"- Split: `{summary.get('split')}`",
        f"- Runtime records: `{summary.get('samples_completed', 0)}` / `{summary.get('samples_requested', 0)}`",
        f"- Production accuracy: `{metrics.get('production_accuracy')}`",
        f"- Accuracy eligible samples: `{metrics.get('eligible_samples', 0)}`",
        f"- Human-verified samples: `{summary.get('human_verified_samples', 0)}`",
        f"- Human-verified real samples (all roles): `{summary.get('real_verified_samples', 0)}`",
        f"- NOT VERIFIED samples: `{summary.get('not_verified_samples', 0)}`",
        f"- Synthetic records: `{summary.get('synthetic_records', 0)}` (reported separately)",
        f"- OCR warmup: `{startup.get('warmup_ms')}` ms (excluded from steady-state timing)",
        f"- Startup: `{startup.get('startup_ms')}` ms",
        "",
        "## Runtime evidence",
        "",
        f"- Quality pass: `{quality.get('quality_pass', 0)}`",
        f"- Quality reject: `{quality.get('quality_reject', 0)}`",
        f"- Potential quality false rejects: `{quality.get('potential_false_reject', 0)}`",
        f"- Sample failures: `{summary.get('sample_failures', 0)}`",
        "",
        "## Ground-truth status",
        "",
        "Accuracy metrics are only production claims when the target records are",
        "human verified. Unverified/synthetic runtime evidence is not",
        "converted into accuracy.",
        "",
        "## Human verification required",
        "",
        "NEEDS_HUMAN_VERIFICATION: confirm NVIDIA P/N, Customer Part Number,",
        "Our Part Number, DataMatrix payload, and expected business status before",
        "running a production accuracy report.",
    ]
    return "\n".join(lines) + "\n"
