"""Reusable dataset evaluator built on the production inspection pipeline."""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ..preprocessing.crop import crop_image
from ..preprocessing.orientation import normalize_orientation
from .dataset import is_valid_image_id
from .metrics import calculate_phase1_metrics
from .reporting import write_phase1_outputs

if TYPE_CHECKING:
    from ..pipeline.inspection import InspectionPipeline


FAILURE_STAGES = {
    "IMAGE_LOAD",
    "ORIENTATION",
    "ROI",
    "QUALITY",
    "OCR",
    "BARCODE",
    "EXTRACTION",
    "VALIDATION",
    "ARTIFACT",
    "UNKNOWN",
}


class EvaluationInitializationError(RuntimeError):
    """Fatal model/decoder initialization error; the run cannot be trusted."""


class EvaluationStageError(RuntimeError):
    """A sample-scoped failure with a stable failure-stage enum."""

    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage if stage in FAILURE_STAGES else "UNKNOWN"
        super().__init__(message)


ImageLoader = Callable[[Path], object]
OrientationFn = Callable[[object, int], object]
ArtifactWriter = Callable[..., dict[str, str]]


class DatasetEvaluator:
    """Run one warmed pipeline over a dataset split in one process."""

    def __init__(
        self,
        *,
        pipeline: "InspectionPipeline",
        dataset: str | Path,
        output: str | Path,
        split: str = "smoke",
        image_id: Optional[str] = None,
        device: str = "gpu:0",
        rotate_degrees: int = 0,
        roi_normalized: bool = True,
        quality_observation: bool = False,
        image_loader: Optional[ImageLoader] = None,
        orientation_fn: Optional[OrientationFn] = None,
        artifact_writer: Optional[ArtifactWriter] = None,
        min_condition_samples: int = 5,
        run_id: Optional[str] = None,
    ) -> None:
        self.pipeline = pipeline
        self.dataset = Path(dataset).expanduser().resolve()
        self.output_root = Path(output).expanduser().resolve()
        self.output = self.output_root
        self.split = split
        self.image_id = image_id
        self.device = device
        self.rotate_degrees = rotate_degrees
        self.roi_normalized = roi_normalized
        self.quality_observation = quality_observation
        self.image_loader = image_loader or _load_image
        self.orientation_fn = orientation_fn or normalize_orientation
        self.artifact_writer = artifact_writer or _write_sample_artifacts
        if min_condition_samples < 1:
            raise ValueError("min_condition_samples must be at least 1")
        self.min_condition_samples = min_condition_samples
        self.requested_run_id = run_id
        self.run_id = ""
        self._runtime_model: dict[str, Any] = {}
        self._dataset_role = ""

    def run(self) -> dict[str, Any]:
        dataset_config = _load_dataset_config(self.dataset)
        self._dataset_role = str(dataset_config.get("dataset_role") or "")
        samples = _load_samples(self.dataset, self.split, self.image_id)
        self.run_id = _normalize_run_id(self.requested_run_id)
        provenance = _build_provenance(
            evaluator=self,
            dataset_config=dataset_config,
        )
        self.output = _create_run_directory(self.output_root, self.run_id)
        provenance_path = self.output / "provenance.json"
        _write_json(provenance_path, provenance)
        lifecycle = self._initialize_runtime()
        self._runtime_model = dict(lifecycle["model"])
        records: list[dict[str, Any]] = []
        for sample in samples:
            try:
                records.append(self._evaluate_sample(sample))
            except EvaluationStageError as exc:
                records.append(self._failure_record(sample, exc.stage, exc))
            except Exception as exc:  # sample isolation boundary
                records.append(self._failure_record(sample, "UNKNOWN", exc))

        validator = self.pipeline.validator
        extractor = getattr(self.pipeline, "extractor", None)
        semantic_blockers = dict(getattr(extractor, "semantic_blockers", {}) or {})
        metrics = calculate_phase1_metrics(
            records,
            dataset_role=self._dataset_role,
            required_fields=tuple(validator.required_fields),
            barcode_required=bool(validator.barcode_required),
            semantic_blockers=semantic_blockers,
            min_condition_samples=self.min_condition_samples,
        )
        failures = [record for record in records if record.get("failure_stage")]
        real_verified = sum(
            record.get("ground_truth_verified") is True and record.get("synthetic") is not True
            for record in records
        )
        summary: dict[str, Any] = {
            "status": "COMPLETED",
            "run_id": self.run_id,
            "run_directory": str(self.output),
            "output_root": str(self.output_root),
            "provenance_path": str(provenance_path),
            "dataset": str(self.dataset),
            "dataset_id": dataset_config.get("dataset_id"),
            "dataset_role": self._dataset_role,
            "split": self.split,
            "image_id_filter": self.image_id,
            "device": self.device,
            "rotate_degrees": self.rotate_degrees,
            "quality_observation": self.quality_observation,
            "samples_requested": len(samples),
            "samples_completed": len(records),
            "sample_failures": len(failures),
            "human_verified_samples": sum(record.get("ground_truth_verified") is True for record in records),
            "real_verified_samples": real_verified,
            "not_verified_samples": len(records) - real_verified,
            "synthetic_records": sum(record.get("synthetic") is True for record in records),
            "startup": lifecycle,
            "provenance": provenance,
            "metrics": metrics,
            "production_accuracy": metrics["production_accuracy"],
            "eligible_samples": metrics["eligible_samples"],
            "samples": [
                {
                    "image_id": record["image_id"],
                    "status": record["prediction"].get("status"),
                    "inference_executed": record["inference_executed"],
                    "ground_truth_verified": record["ground_truth_verified"],
                    "included_in_accuracy_metrics": record["included_in_accuracy_metrics"],
                    "failure_stage": record.get("failure_stage"),
                    "synthetic": record.get("synthetic", False),
                    "artifacts": record.get("artifacts"),
                }
                for record in records
            ],
        }
        self.output.mkdir(parents=True, exist_ok=True)
        report_paths = write_phase1_outputs(
            self.output,
            summary=summary,
            metrics=metrics,
        )
        summary["report_paths"] = report_paths
        _write_json(self.output / "run_summary.json", summary)
        _write_json(self.output / "summary.json", {**summary, "report_paths": report_paths})
        return summary

    def _initialize_runtime(self) -> dict[str, Any]:
        started = time.perf_counter()
        warmup_started = time.perf_counter()
        try:
            warmup = self.pipeline.ocr.warmup()
        except Exception as exc:
            raise EvaluationInitializationError("OCR_WARMUP_ERROR") from exc
        warmup_ms = float(
            getattr(self.pipeline.ocr, "warmup_ms", 0.0)
            or getattr(warmup, "elapsed_ms", 0.0)
            or ((time.perf_counter() - warmup_started) * 1000)
        )
        ocr_ready = bool(getattr(warmup, "success", False) and getattr(self.pipeline.ocr, "ready", False))
        if not ocr_ready:
            raise EvaluationInitializationError("OCR_NOT_READY")

        try:
            zxing_ready = bool(self.pipeline.barcode.prepare())
        except Exception as exc:
            raise EvaluationInitializationError("ZXING_INIT_ERROR") from exc
        if not zxing_ready:
            raise EvaluationInitializationError("ZXING_NOT_READY")

        return {
            "ocr_ready": ocr_ready,
            "zxing_ready": zxing_ready,
            "warmup_ms": warmup_ms,
            "startup_ms": (time.perf_counter() - started) * 1000,
            "warmup_excluded_from_sample_timings": True,
            "model": {
                "engine": getattr(self.pipeline.ocr, "engine", None),
                "backend": getattr(self.pipeline.ocr, "backend", None),
                "device": getattr(self.pipeline.ocr, "device", self.device),
                "version": getattr(self.pipeline.ocr, "ocr_version", None),
                "resident": True,
            },
        }

    def _evaluate_sample(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        image_id = str(sample["image_id"])
        sample_dir = _safe_sample_directory(self.output, image_id)
        image_path = self.dataset / str(sample["image_path"])
        load_started = time.perf_counter()
        try:
            image = self.image_loader(image_path)
        except Exception as exc:
            raise EvaluationStageError("IMAGE_LOAD", "IMAGE_LOAD_ERROR") from exc
        image_load_ms = (time.perf_counter() - load_started) * 1000

        orientation_started = time.perf_counter()
        try:
            processed_image = self.orientation_fn(image, self.rotate_degrees)
        except Exception as exc:
            raise EvaluationStageError("ORIENTATION", "ORIENTATION_ERROR") from exc
        orientation_ms = (time.perf_counter() - orientation_started) * 1000

        try:
            production_result = self.pipeline.inspect_frame(
                processed_image,
                event_id=image_id,
                frame_id=0,
            )
        except Exception as exc:
            raise EvaluationStageError("UNKNOWN", "PIPELINE_ERROR") from exc

        observation_result = None
        if (
            self.quality_observation
            and production_result.quality.status != "PASS"
            and production_result.label is not None
        ):
            try:
                observation_result = self.pipeline.inspect_frame(
                    processed_image,
                    event_id=f"{image_id}-observation",
                    frame_id=0,
                    quality_observation=True,
                )
            except Exception as exc:
                raise EvaluationStageError("QUALITY", "QUALITY_OBSERVATION_ERROR") from exc

        evidence_result = observation_result or production_result
        stage_timings = _stage_timings(
            result=evidence_result,
            image_load_ms=image_load_ms,
            orientation_ms=orientation_ms,
        )
        prediction = _prediction_payload(
            image_id=image_id,
            production_result=production_result,
            evidence_result=evidence_result,
            observation_result=observation_result,
            stage_timings=stage_timings,
            rotate_degrees=self.rotate_degrees,
            roi_normalized=self.roi_normalized,
            runtime_model=self._runtime_model,
        )
        label_crop = _label_crop(processed_image, evidence_result)
        try:
            artifacts = self.artifact_writer(
                sample_dir=sample_dir,
                input_image=processed_image,
                label_crop=label_crop,
                prediction=prediction,
                ocr_lines=[line.to_dict() for line in evidence_result.raw_ocr.lines],
                barcode=_barcode_payload(evidence_result),
                stage_timings=stage_timings,
            )
            _assert_artifacts_within_root(artifacts, self.output)
        except Exception as exc:
            raise EvaluationStageError("ARTIFACT", "ARTIFACT_WRITE_ERROR") from exc
        prediction["artifacts"] = artifacts
        verified = sample.get("human_verified") is True
        accuracy_eligible = self._accuracy_eligible(sample)
        failure_stage = _result_failure_stage(evidence_result)
        prediction.update(
            {
                "inference_executed": True,
                "ground_truth_verified": verified,
                "included_in_accuracy_metrics": accuracy_eligible,
                "synthetic": sample.get("synthetic") is True,
                "conditions": dict(sample.get("conditions") or {}),
                "failure_stage": failure_stage,
            }
        )
        _write_json(sample_dir / "prediction.json", prediction)
        record = {
            "image_id": image_id,
            "expected": dict(sample.get("expected") or {}),
            "conditions": dict(sample.get("conditions") or {}),
            "synthetic": sample.get("synthetic") is True,
            "prediction": prediction,
            "inference_executed": True,
            "ground_truth_verified": verified,
            "included_in_accuracy_metrics": accuracy_eligible,
            "failure_stage": failure_stage,
            "artifacts": artifacts,
        }
        return record

    def _failure_record(
        self,
        sample: Mapping[str, Any],
        failure_stage: str,
        error: Exception,
    ) -> dict[str, Any]:
        image_id = str(sample.get("image_id", "UNKNOWN"))
        sample_dir = _safe_sample_directory(self.output, image_id)
        accuracy_eligible = self._accuracy_eligible(sample)
        prediction = {
            "image_id": image_id,
            "status": "ERROR",
            "business_status": "ERROR",
            "fields": {},
            "barcode": {"selected": None, "items": [], "status": "NOT_RUN"},
            "quality": None,
            "roi": None,
            "model": {},
            "timings": {},
            "errors": {
                "failure_stage": failure_stage,
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
            "inference_executed": failure_stage not in {"IMAGE_LOAD", "ORIENTATION"},
            "ground_truth_verified": sample.get("human_verified") is True,
            "included_in_accuracy_metrics": accuracy_eligible,
        }
        sample_dir.mkdir(parents=True, exist_ok=True)
        _write_json(sample_dir / "prediction.json", prediction)
        _write_json(sample_dir / "stage_timings.json", {})
        return {
            "image_id": image_id,
            "expected": dict(sample.get("expected") or {}),
            "conditions": dict(sample.get("conditions") or {}),
            "synthetic": sample.get("synthetic") is True,
            "prediction": prediction,
            "inference_executed": prediction["inference_executed"],
            "ground_truth_verified": prediction["ground_truth_verified"],
            "included_in_accuracy_metrics": accuracy_eligible,
            "failure_stage": failure_stage,
            "failure": prediction["errors"],
            "artifacts": {"directory": str(sample_dir)},
        }

    def _accuracy_eligible(self, sample: Mapping[str, Any]) -> bool:
        return bool(
            self._dataset_role == "target"
            and sample.get("human_verified") is True
            and sample.get("synthetic") is not True
        )


def _load_dataset_config(dataset: Path) -> dict[str, Any]:
    path = dataset / "dataset_config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dataset_config.json root must be an object")
    role = payload.get("dataset_role")
    if role not in {"target", "robustness", "public"}:
        raise ValueError(f"INVALID_DATASET_ROLE: {role!r}")
    return payload


def _normalize_run_id(requested: Optional[str]) -> str:
    run_id = requested or (
        "RUN_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + "_"
        + uuid.uuid4().hex[:8].upper()
    )
    if not is_valid_image_id(run_id):
        raise ValueError(f"INVALID_RUN_ID: {run_id!r}")
    return run_id


def _create_run_directory(output_root: Path, run_id: str) -> Path:
    root = output_root.expanduser().resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root:
        raise ValueError(f"RUN_PATH_OUTSIDE_OUTPUT: {candidate}")
    root.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        raise FileExistsError(f"RUN_DIRECTORY_EXISTS: {candidate}")
    candidate.mkdir()
    return candidate


def _build_provenance(
    *,
    evaluator: DatasetEvaluator,
    dataset_config: Mapping[str, Any],
) -> dict[str, Any]:
    config_values = _evaluation_config_values(evaluator)
    extractor = getattr(evaluator.pipeline, "extractor", None)
    roi = config_values["roi"]
    quality_thresholds = config_values["quality_thresholds"]
    return {
        "schema_version": "1.0",
        "run_id": evaluator.run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git": _git_provenance(evaluator.dataset),
        "dataset": {
            "path": str(evaluator.dataset),
            "dataset_id": dataset_config.get("dataset_id"),
            "dataset_role": dataset_config.get("dataset_role"),
            "fingerprint_sha256": _dataset_fingerprint(evaluator.dataset),
        },
        "evaluation_config": {
            "fingerprint_sha256": _json_fingerprint(config_values),
            "values": config_values,
        },
        "roi": roi,
        "quality_thresholds": quality_thresholds,
        "dependencies": _dependency_versions(),
        "extractor": {
            "profile_name": getattr(extractor, "profile_name", "unknown"),
            "profile_version": getattr(extractor, "profile_version", "unknown"),
            "mapping_summary": dict(getattr(extractor, "mapping_summary", {}) or {}),
            "semantic_blockers": dict(getattr(extractor, "semantic_blockers", {}) or {}),
        },
    }


def _evaluation_config_values(evaluator: DatasetEvaluator) -> dict[str, Any]:
    detector = getattr(evaluator.pipeline, "detector", None)
    quality = getattr(evaluator.pipeline, "quality_checker", None)
    validator = getattr(evaluator.pipeline, "validator", None)
    extractor = getattr(evaluator.pipeline, "extractor", None)
    roi_value = getattr(detector, "roi", None)
    roi = {
        "detector": getattr(detector, "name", type(detector).__name__ if detector else None),
        "coordinates": list(roi_value) if roi_value is not None else None,
        "normalized": getattr(detector, "normalized", evaluator.roi_normalized),
        "rotate_degrees": evaluator.rotate_degrees,
    }
    quality_names = (
        "min_width",
        "min_height",
        "min_brightness",
        "max_brightness",
        "min_sharpness",
        "max_underexposed_ratio",
        "max_overexposed_ratio",
        "max_glare_ratio",
    )
    quality_thresholds = {
        name: getattr(quality, name, None) for name in quality_names
    }
    return {
        "split": evaluator.split,
        "image_id_filter": evaluator.image_id,
        "device": evaluator.device,
        "quality_observation": evaluator.quality_observation,
        "min_condition_samples": evaluator.min_condition_samples,
        "roi": roi,
        "quality_thresholds": quality_thresholds,
        "validator": {
            "required_fields": list(getattr(validator, "required_fields", ())),
            "barcode_required": bool(getattr(validator, "barcode_required", False)),
            "min_field_confidence": getattr(validator, "min_field_confidence", None),
        },
        "extractor": {
            "profile_name": getattr(extractor, "profile_name", "unknown"),
            "profile_version": getattr(extractor, "profile_version", "unknown"),
            "mapping_summary": dict(getattr(extractor, "mapping_summary", {}) or {}),
            "semantic_blockers": dict(getattr(extractor, "semantic_blockers", {}) or {}),
        },
    }


def _dataset_fingerprint(dataset: Path) -> str:
    root = dataset.resolve()
    paths = {
        root / "dataset_config.json",
        root / "ground_truth.json",
    }
    manifest = root / "manifest.csv"
    if manifest.exists():
        paths.add(manifest)
    try:
        ground_truth = json.loads((root / "ground_truth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        ground_truth = {}
    for sample in ground_truth.get("images", []) if isinstance(ground_truth, Mapping) else []:
        relative = sample.get("image_path") if isinstance(sample, Mapping) else None
        if not isinstance(relative, str):
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        paths.add(candidate)
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item.relative_to(root))):
        relative = str(path.relative_to(root)).replace("\\", "/")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if not path.is_file():
            digest.update(b"MISSING")
            continue
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _json_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_provenance(start: Path) -> dict[str, Any]:
    repo = _find_git_root(start)
    if repo is None:
        return {"repository": None, "commit": None, "dirty": None}
    commit = _run_git(repo, "rev-parse", "HEAD")
    status = _run_git(repo, "status", "--porcelain", "--untracked-files=normal")
    return {
        "repository": str(repo),
        "commit": commit or None,
        "dirty": bool(status) if status is not None else None,
    }


def _find_git_root(start: Path) -> Path | None:
    candidates = [start.resolve(), Path.cwd().resolve()]
    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists():
                return parent
    return None


def _run_git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _dependency_versions() -> dict[str, Any]:
    dependencies: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    for distribution in (
        "numpy",
        "opencv-python",
        "paddleocr",
        "paddlepaddle",
        "paddlex",
        "torch",
        "transformers",
        "safetensors",
        "zxing-cpp",
    ):
        try:
            dependencies[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            dependencies[distribution] = None
    return dependencies


def _load_samples(dataset: Path, split: str, image_id: Optional[str]) -> list[dict[str, Any]]:
    payload = json.loads((dataset / "ground_truth.json").read_text(encoding="utf-8"))
    manifest_path = dataset / "manifest.csv"
    manifest: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        with manifest_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                manifest[row.get("image_id", "")] = row
    samples = []
    for raw_sample in payload.get("images", []):
        raw_image_id = raw_sample.get("image_id")
        if not is_valid_image_id(raw_image_id):
            raise ValueError(f"INVALID_IMAGE_ID: {raw_image_id!r}")
        if raw_sample.get("split") != split:
            continue
        sample = dict(raw_sample)
        row = manifest.get(str(sample.get("image_id")), {})
        sample["synthetic"] = str(row.get("synthetic", "false")).strip().lower() in {"true", "1", "yes"}
        samples.append(sample)
    if image_id is not None:
        samples = [sample for sample in samples if sample.get("image_id") == image_id]
    if image_id is not None and not samples:
        raise ValueError(f"image_id not found in split {split}: {image_id}")
    return samples


def _safe_sample_directory(output: Path, image_id: str) -> Path:
    if not is_valid_image_id(image_id):
        raise ValueError(f"INVALID_IMAGE_ID: {image_id!r}")
    root = output.expanduser().resolve()
    samples_root = (root / "samples").resolve()
    candidate = (samples_root / image_id).resolve()
    if candidate.parent != samples_root:
        raise ValueError(f"ARTIFACT_PATH_OUTSIDE_OUTPUT: {candidate}")
    return candidate


def _assert_artifacts_within_root(
    artifacts: Mapping[str, str],
    output: Path,
) -> None:
    root = output.expanduser().resolve()
    for name, raw_path in artifacts.items():
        if not isinstance(raw_path, str):
            raise ValueError(f"INVALID_ARTIFACT_PATH: {name}")
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"ARTIFACT_PATH_OUTSIDE_OUTPUT: {name}={path}"
            ) from exc


def _load_image(path: Path) -> object:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise EvaluationStageError("IMAGE_LOAD", "OpenCV is required") from exc
    image = cv2.imread(str(path))
    if image is None or getattr(image, "size", 0) == 0:
        raise EvaluationStageError("IMAGE_LOAD", f"Could not decode image: {path.name}")
    return image


def _label_crop(image: object, result: Any) -> object | None:
    if result.crop_bbox is None:
        return None
    return crop_image(image, result.crop_bbox, padding_ratio=0.0).image


def _stage_timings(*, result: Any, image_load_ms: float, orientation_ms: float) -> dict[str, float]:
    internal = dict(getattr(result, "timing", {}) or {})
    crop_ms = float(internal.get("crop_rectify_ms", 0.0))
    return {
        "image_load_ms": image_load_ms,
        "preprocessing_ms": orientation_ms + crop_ms,
        "roi_ms": float(internal.get("detection_ms", 0.0)),
        "quality_ms": float(internal.get("quality_ms", 0.0)),
        "ocr_ms": float(internal.get("ocr_ms", 0.0)),
        "barcode_ms": float(internal.get("barcode_ms", 0.0)),
        "extraction_ms": float(internal.get("field_extraction_ms", 0.0)),
        "validation_ms": float(internal.get("validation_ms", 0.0)),
        "total_pipeline_ms": image_load_ms + orientation_ms + float(internal.get("total_ms", 0.0)),
        "frame_selection_ms": float(internal.get("frame_selection_ms", 0.0)),
        "crop_rectify_ms": crop_ms,
        "candidate_ranking_ms": float(internal.get("candidate_ranking_ms", 0.0)),
    }


def _prediction_payload(
    *,
    image_id: str,
    production_result: Any,
    evidence_result: Any,
    observation_result: Any,
    stage_timings: dict[str, float],
    rotate_degrees: int,
    roi_normalized: bool,
    runtime_model: Mapping[str, Any],
) -> dict[str, Any]:
    label = evidence_result.label
    production_validation = production_result.validation.to_dict()
    payload: dict[str, Any] = {
        "image_id": image_id,
        "status": production_result.validation.status,
        "business_status": production_result.validation.status,
        "production_decision": production_validation,
        "fields": {key: value.to_dict() for key, value in evidence_result.extracted.items()},
        "evidence": [
            item.to_dict() for item in getattr(evidence_result, "evidence", [])
        ],
        "ocr": evidence_result.raw_ocr.to_dict(),
        "barcode": _barcode_payload(evidence_result),
        "quality": production_result.quality.to_dict(),
        "roi": {
            "detector": label.detector if label is not None else None,
            "label_bbox": list(label.bbox) if label is not None else None,
            "crop_bbox": list(evidence_result.crop_bbox) if evidence_result.crop_bbox is not None else None,
            "normalized": roi_normalized if label is not None else None,
            "rotate_degrees": rotate_degrees,
        },
        "model": {
            "engine": evidence_result.raw_ocr.engine or runtime_model.get("engine"),
            "backend": evidence_result.raw_ocr.backend or runtime_model.get("backend"),
            "device": evidence_result.raw_ocr.device or runtime_model.get("device"),
            "version": evidence_result.raw_ocr.model or runtime_model.get("version"),
            "resident": True,
        },
        "timings": stage_timings,
        "errors": {
            "pipeline": evidence_result.error,
            "ocr": evidence_result.raw_ocr.error_code or evidence_result.raw_ocr.error,
            "barcode": evidence_result.barcode.error_code or evidence_result.barcode.error,
        },
    }
    if observation_result is not None:
        payload["observation_result"] = _observation_payload(observation_result)
    return payload


def _observation_payload(result: Any) -> dict[str, Any]:
    return {
        "status": result.validation.status,
        "validation": result.validation.to_dict(),
        "ocr": result.raw_ocr.to_dict(),
        "fields": {key: value.to_dict() for key, value in result.extracted.items()},
        "evidence": [item.to_dict() for item in getattr(result, "evidence", [])],
        "barcode": _barcode_payload(result),
        "quality": result.quality.to_dict(),
        "timings": dict(result.timing),
    }


def _barcode_payload(result: Any) -> dict[str, Any]:
    items = [item.to_dict() for item in getattr(result, "barcodes", [])]
    selected = result.barcode.to_dict()
    if not items and selected.get("value"):
        items = [selected]
    return {
        "status": selected.get("status"),
        "number_found": len(items),
        "items": items,
        "selected": selected,
    }


def _result_failure_stage(result: Any) -> str | None:
    """Convert adapter/runtime failure states to the evaluator enum.

    A normal quality rejection is evidence, not a runner failure; it remains
    represented by the production status and quality reasons.
    """

    raw_ocr = getattr(result, "raw_ocr", None)
    if raw_ocr is not None and getattr(raw_ocr, "state", None) == "FAILED":
        return "OCR"
    barcode = getattr(result, "barcode", None)
    if barcode is not None and getattr(barcode, "state", None) == "FAILED":
        return "BARCODE"
    error = getattr(result, "error", None)
    if error in {"DETECTION_RUNTIME_ERROR", "CROP_PREPARATION_ERROR", "LABEL_NOT_DETECTED"}:
        return "ROI"
    if error == "QUALITY_RUNTIME_ERROR":
        return "QUALITY"
    return None


def _write_sample_artifacts(
    *,
    sample_dir: Path,
    input_image: object,
    label_crop: object | None,
    prediction: dict[str, Any],
    ocr_lines: list[dict[str, Any]],
    barcode: dict[str, Any],
    stage_timings: dict[str, float],
) -> dict[str, str]:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("OpenCV is required to write evaluation artifacts") from exc
    sample_dir.mkdir(parents=True, exist_ok=True)
    input_path = sample_dir / "input.jpg"
    if not cv2.imwrite(str(input_path), input_image):
        raise RuntimeError("INPUT_ARTIFACT_WRITE_FAILED")
    artifacts = {"directory": str(sample_dir), "input": str(input_path)}
    if label_crop is not None:
        crop_path = sample_dir / "label_crop.jpg"
        if not cv2.imwrite(str(crop_path), label_crop):
            raise RuntimeError("LABEL_CROP_ARTIFACT_WRITE_FAILED")
        artifacts["label_crop"] = str(crop_path)
    _write_json(sample_dir / "ocr_lines.json", ocr_lines)
    _write_json(sample_dir / "barcode.json", barcode)
    _write_json(sample_dir / "stage_timings.json", stage_timings)
    artifacts.update(
        {
            "prediction": str(sample_dir / "prediction.json"),
            "ocr_lines": str(sample_dir / "ocr_lines.json"),
            "barcode": str(sample_dir / "barcode.json"),
            "stage_timings": str(sample_dir / "stage_timings.json"),
        }
    )
    _write_json(sample_dir / "prediction.json", {**prediction, "artifacts": artifacts})
    return artifacts


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _render_report(summary: Mapping[str, Any]) -> str:
    startup = summary["startup"]
    metrics = summary["metrics"]
    lines = [
        "# Evaluation report",
        "",
        f"- Status: `{summary['status']}`",
        f"- Dataset: `{summary['dataset']}`",
        f"- Split: `{summary['split']}`",
        f"- Samples completed: `{summary['samples_completed']}` / `{summary['samples_requested']}`",
        f"- Sample failures: `{summary['sample_failures']}`",
        f"- OCR warmup: `{startup['warmup_ms']:.3f} ms` (excluded from sample timings)",
        f"- Startup: `{startup['startup_ms']:.3f} ms`",
        f"- Accuracy status: `{metrics['accuracy_status']}`",
        f"- Accuracy-eligible samples: `{metrics['eligible_samples']}`",
        "",
        "No production accuracy claim is made unless samples are human verified.",
        "",
        "## Samples",
        "",
        "| image_id | status | verified | included | failure_stage |",
        "|---|---|---:|---:|---|",
    ]
    for sample in summary["samples"]:
        lines.append(
            f"| {sample['image_id']} | {sample['status']} | "
            f"{sample['ground_truth_verified']} | {sample['included_in_accuracy_metrics']} | "
            f"{sample['failure_stage'] or ''} |"
        )
    return "\n".join(lines) + "\n"
