import json
from pathlib import Path
from types import SimpleNamespace

from label_inspection.evaluation.evaluator import DatasetEvaluator
from label_inspection.schemas import (
    BarcodeResult,
    ExtractedField,
    InspectionResult,
    LabelCandidate,
    OCRLine,
    QualityReport,
    RawOCRResult,
    ValidationResult,
    STAGE_NOT_RUN,
)


class DummyImage:
    shape = (100, 120, 3)

    def __getitem__(self, _key):
        return self


class FakeOCR:
    engine = "ppocr_v6"
    backend = "transformers"
    device = "gpu:0"
    ocr_version = "PP-OCRv6"
    warmup_ms = 12.5
    ready = False

    def __init__(self):
        self.warmup_calls = 0

    def warmup(self):
        self.warmup_calls += 1
        self.ready = True
        return RawOCRResult(engine=self.engine, success=True, lines=[])


class FakeBarcode:
    def prepare(self):
        return True


class FakePipeline:
    def __init__(self, *, quality_reject=False, fail_paths=None):
        self.ocr = FakeOCR()
        self.barcode = FakeBarcode()
        self.quality_reject = quality_reject
        self.fail_paths = set(fail_paths or [])
        self.calls = []
        self.validator = SimpleNamespace(
            required_fields=("customer_part_number",),
            barcode_required=False,
            min_field_confidence=0.7,
            field_patterns={},
        )
        self.extractor = SimpleNamespace(
            profile_name="dgx_spark_label",
            profile_version="1.0-test",
            semantic_blockers={
                "customer_part_number": "KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION"
            },
            mapping_summary={
                "customer_part_number": "Nvidia P/N -> customer_part_number"
            },
        )
        self.detector = SimpleNamespace(
            name="FixedROI", roi=(0.1, 0.2, 0.9, 0.8), normalized=True
        )
        self.quality_checker = SimpleNamespace(
            min_width=32,
            min_height=16,
            min_brightness=20.0,
            max_brightness=245.0,
            min_sharpness=50.0,
            max_underexposed_ratio=0.3,
            max_overexposed_ratio=0.85,
            max_glare_ratio=0.8,
        )

    def inspect_frame(self, image, *, event_id, frame_id=0, quality_observation=False):
        self.calls.append((event_id, quality_observation))
        quality_fail = self.quality_reject and not quality_observation
        quality = QualityReport(
            status="FAIL" if quality_fail else "PASS",
            reasons=("GLARE",) if quality_fail else (),
        )
        raw = RawOCRResult(
            engine="ppocr_v6",
            lines=[] if quality_fail else [OCRLine("CP-01", 0.99)],
            success=not quality_fail,
            state=STAGE_NOT_RUN if quality_fail else None,
        )
        barcode = BarcodeResult(value=None if quality_fail else "DM-01", success=not quality_fail, state=STAGE_NOT_RUN if quality_fail else None)
        extracted = {} if quality_fail else {"customer_part_number": ExtractedField("CP-01", 0.99, "ppocr_v6", "CP-01")}
        return InspectionResult(
            event_id=event_id,
            camera_id="TEST",
            frame_id=0,
            label=LabelCandidate((10.0, 10.0, 110.0, 90.0), detector="FixedROI"),
            crop_bbox=(10.0, 10.0, 110.0, 90.0),
            raw_ocr=raw,
            extracted=extracted,
            barcode=barcode,
            barcodes=[barcode] if barcode.value else [],
            quality=quality,
            validation=ValidationResult(
                status="REVIEW" if quality_fail else "PASS",
                reasons=("QUALITY_GLARE",) if quality_fail else (),
            ),
            timing={
                "frame_selection_ms": 1.0,
                "detection_ms": 2.0,
                "crop_rectify_ms": 3.0,
                "quality_ms": 4.0,
                "ocr_ms": 5.0 if not quality_fail else 0.0,
                "barcode_ms": 6.0 if not quality_fail else 0.0,
                "field_extraction_ms": 0.5 if not quality_fail else 0.0,
                "validation_ms": 0.5,
                "candidate_ranking_ms": 0.2,
                "total_ms": 20.0,
            },
        )


def _dataset(tmp_path: Path, *, count=1, verified=False):
    images = []
    for index in range(count):
        image_id = f"SAMPLE_{index + 1:04d}"
        image_path = f"images/{image_id}.jpg"
        images.append(
            {
                "image_id": image_id,
                "image_path": image_path,
                "split": "smoke",
                "human_verified": verified,
                "expected": {"customer_part_number": "CP-01"},
            }
        )
    (tmp_path / "images").mkdir()
    (tmp_path / "dataset_config.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "dataset_id": "test_dataset",
                "dataset_role": "target",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ground_truth.json").write_text(json.dumps({"images": images}), encoding="utf-8")
    return images


def _writer(**kwargs):
    directory = kwargs["sample_dir"]
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("input.jpg", "label_crop.jpg", "ocr_lines.json", "barcode.json", "stage_timings.json"):
        (directory / name).write_text("{}", encoding="utf-8")
    return {"directory": str(directory), "input": str(directory / "input.jpg")}


def test_evaluator_isolates_one_sample_failure(tmp_path):
    samples = _dataset(tmp_path, count=2)

    def loader(path):
        if path.name.endswith("0002.jpg"):
            raise OSError("bad image")
        return DummyImage()

    summary = DatasetEvaluator(
        pipeline=FakePipeline(),
        dataset=tmp_path,
        output=tmp_path / "results",
        image_loader=loader,
        artifact_writer=_writer,
    ).run()
    assert summary["samples_completed"] == 2
    assert summary["sample_failures"] == 1
    assert summary["samples"][1]["failure_stage"] == "IMAGE_LOAD"


def test_evaluator_writes_schema_and_stage_timings(tmp_path):
    samples = _dataset(tmp_path)
    summary = DatasetEvaluator(
        pipeline=FakePipeline(),
        dataset=tmp_path,
        output=tmp_path / "results",
        image_loader=lambda _path: DummyImage(),
        artifact_writer=_writer,
    ).run()
    sample_dir = Path(summary["run_directory"]) / "samples" / samples[0]["image_id"]
    prediction = json.loads((sample_dir / "prediction.json").read_text(encoding="utf-8"))
    assert prediction["image_id"] == samples[0]["image_id"]
    assert "fields" in prediction and "barcode" in prediction and "quality" in prediction
    timings = prediction["timings"]
    for key in ("image_load_ms", "preprocessing_ms", "roi_ms", "quality_ms", "ocr_ms", "barcode_ms", "extraction_ms", "validation_ms", "total_pipeline_ms"):
        assert key in timings
    assert summary["startup"]["warmup_excluded_from_sample_timings"] is True


def test_quality_observation_preserves_production_status(tmp_path):
    samples = _dataset(tmp_path)
    pipeline = FakePipeline(quality_reject=True)
    summary = DatasetEvaluator(
        pipeline=pipeline,
        dataset=tmp_path,
        output=tmp_path / "results",
        quality_observation=True,
        image_loader=lambda _path: DummyImage(),
        artifact_writer=_writer,
    ).run()
    prediction = json.loads(
        (Path(summary["run_directory"]) / "samples" / samples[0]["image_id"] / "prediction.json").read_text(encoding="utf-8")
    )
    assert prediction["status"] == "REVIEW"
    assert prediction["production_decision"]["status"] == "REVIEW"
    assert prediction["observation_result"]["ocr"]["success"] is True
    assert pipeline.calls == [("SAMPLE_0001", False), ("SAMPLE_0001-observation", True)]


def test_evaluator_writes_complete_phase1_report_contract(tmp_path):
    samples = _dataset(tmp_path)
    output = tmp_path / "results"
    summary = DatasetEvaluator(
        pipeline=FakePipeline(),
        dataset=tmp_path,
        output=output,
        image_loader=lambda _path: DummyImage(),
        artifact_writer=_writer,
    ).run()
    required = {
        "summary.json",
        "field_metrics.csv",
        "barcode_metrics.json",
        "business_metrics.json",
        "roi_metrics.json",
        "quality_metrics.json",
        "condition_metrics.csv",
        "latency_metrics.json",
        "failures.csv",
        "evaluation_report.md",
    }
    run_directory = Path(summary["run_directory"])
    assert required.issubset({path.name for path in run_directory.iterdir()})
    persisted = json.loads((run_directory / "summary.json").read_text(encoding="utf-8"))
    assert persisted["production_accuracy"] == "NOT_VERIFIED"
    assert persisted["eligible_samples"] == 0


def test_evaluator_warms_once_for_multiple_samples(tmp_path):
    _dataset(tmp_path, count=2)
    pipeline = FakePipeline()
    DatasetEvaluator(
        pipeline=pipeline,
        dataset=tmp_path,
        output=tmp_path / "results",
        image_loader=lambda _path: DummyImage(),
        artifact_writer=_writer,
    ).run()
    assert pipeline.ocr.warmup_calls == 1


def test_evaluator_rejects_unsafe_image_id_before_runtime_or_artifact_write(tmp_path):
    images = _dataset(tmp_path)
    images[0]["image_id"] = "../../ESCAPE"
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"images": images}), encoding="utf-8"
    )
    pipeline = FakePipeline()

    try:
        DatasetEvaluator(
            pipeline=pipeline,
            dataset=tmp_path,
            output=tmp_path / "results",
            image_loader=lambda _path: DummyImage(),
            artifact_writer=_writer,
        ).run()
    except ValueError as exc:
        assert "INVALID_IMAGE_ID" in str(exc)
    else:
        raise AssertionError("unsafe image_id must fail before evaluation")

    assert pipeline.ocr.warmup_calls == 0
    assert not (tmp_path / "ESCAPE").exists()


def test_evaluator_keeps_verified_runtime_failure_accuracy_eligible(tmp_path):
    images = _dataset(tmp_path, count=2, verified=True)

    def loader(path):
        if path.name.endswith("0002.jpg"):
            raise OSError("bad image")
        return DummyImage()

    summary = DatasetEvaluator(
        pipeline=FakePipeline(),
        dataset=tmp_path,
        output=tmp_path / "results",
        image_loader=loader,
        artifact_writer=_writer,
    ).run()

    failed = summary["samples"][1]
    assert failed["image_id"] == images[1]["image_id"]
    assert failed["failure_stage"] == "IMAGE_LOAD"
    assert failed["included_in_accuracy_metrics"] is True
    assert summary["eligible_samples"] == 2


def test_evaluator_isolates_run_and_writes_reproducibility_provenance(tmp_path):
    _dataset(tmp_path, verified=True)
    output_root = tmp_path / "results"

    summary = DatasetEvaluator(
        pipeline=FakePipeline(),
        dataset=tmp_path,
        output=output_root,
        run_id="RUN_TEST_001",
        image_loader=lambda _path: DummyImage(),
        artifact_writer=_writer,
        min_condition_samples=7,
    ).run()

    run_directory = output_root / "RUN_TEST_001"
    provenance = json.loads(
        (run_directory / "provenance.json").read_text(encoding="utf-8")
    )
    assert summary["run_id"] == "RUN_TEST_001"
    assert Path(summary["run_directory"]) == run_directory.resolve()
    assert not (output_root / "summary.json").exists()
    assert (run_directory / "summary.json").exists()
    assert len(provenance["dataset"]["fingerprint_sha256"]) == 64
    assert len(provenance["evaluation_config"]["fingerprint_sha256"]) == 64
    assert {"commit", "dirty"}.issubset(provenance["git"])
    assert provenance["evaluation_config"]["values"]["min_condition_samples"] == 7
    assert provenance["roi"]["coordinates"] == [0.1, 0.2, 0.9, 0.8]
    assert provenance["quality_thresholds"]["max_glare_ratio"] == 0.8
    assert provenance["dependencies"]["python"]
    assert provenance["extractor"]["profile_version"] == "1.0-test"
    assert "Nvidia P/N" in provenance["extractor"]["mapping_summary"]["customer_part_number"]
    assert provenance["extractor"]["semantic_blockers"]["customer_part_number"].startswith(
        "KNOWN_SEMANTIC_BLOCKER"
    )


def test_evaluator_refuses_to_reuse_or_escape_run_directory(tmp_path):
    _dataset(tmp_path)
    output_root = tmp_path / "results"
    common = {
        "pipeline": FakePipeline(),
        "dataset": tmp_path,
        "output": output_root,
        "image_loader": lambda _path: DummyImage(),
        "artifact_writer": _writer,
    }
    DatasetEvaluator(run_id="RUN_FIXED", **common).run()

    try:
        DatasetEvaluator(run_id="RUN_FIXED", **common).run()
    except FileExistsError as exc:
        assert "RUN_DIRECTORY_EXISTS" in str(exc)
    else:
        raise AssertionError("run output must never be overwritten")

    try:
        DatasetEvaluator(run_id="../ESCAPE", **common).run()
    except ValueError as exc:
        assert "INVALID_RUN_ID" in str(exc)
    else:
        raise AssertionError("run_id must not escape output root")
