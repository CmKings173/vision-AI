# Phase 2 Checkpoint 2A Report - Contracts and Boundaries

Historical checkpoint snapshot. Checkpoint 2B evidence now lives in
`tasks/phase2_checkpoint_2b_report.md`.

Status: **IMPLEMENTED AND TESTED LOCALLY — WAITING FOR HUMAN 2A REVIEW**

At the time of this historical checkpoint, no 2B implementation had started.

## CHANGED FILES

Documentation:

- `tasks/phase2_production_foundation_technical_design.md`
- `tasks/phase2_production_foundation_audit.md`
- `tasks/phase2_production_foundation_plan.md`
- `tasks/phase2_production_foundation_todo.md`
- `tasks/phase2_checkpoint_2a_report.md`

Contracts and boundaries:

- `src/label_inspection/contracts/__init__.py`
- `src/label_inspection/contracts/core.py`
- `src/label_inspection/contracts/job.py`
- `src/label_inspection/contracts/result.py`
- `src/label_inspection/pipeline/types.py`
- `src/label_inspection/station/__init__.py`
- `src/label_inspection/station/preparation.py`
- `src/label_inspection/worker/__init__.py`
- `src/label_inspection/worker/processor.py`

Compatibility/configuration:

- `src/label_inspection/app.py`
- `src/label_inspection/config.py`
- `src/label_inspection/pipeline/__init__.py`
- `src/label_inspection/pipeline/inspection.py`

Tests:

- `tests/test_phase2_contracts.py`
- `tests/test_phase2_boundaries.py`

## IMPLEMENTED

- Canonical repo-relative Phase 2 technical design with approved clarifications.
- UUIDv4 `event_id`/`trigger_id` generation and strict distributed UUID validation.
- Integer epoch-ms validation and explicit `received_at_ms`/optional `source_timestamp_ms` semantics.
- Buffered-frame rule: `received_at_ms` may precede manual `triggered_at_ms`; preparation must follow both.
- Versioned `inspection-job.v1` and `inspection-result.v1` contracts.
- Immutable in-memory job metadata, strict unknown-field rejection and artifact references without image bytes/credentials.
- Separate processing, business and delivery enums with documented ownership.
- Structured inspection error contract.
- Quality-rejected terminal result: `COMPLETED + REVIEW`, no inference.
- Preparation-error terminal result: `ERROR + null`, no business fail/inference.
- `StationPreparer` owns selection, orientation, FixedROI, crop, quality and exact-crop copy.
- `InspectionProcessor` owns sequential OCR, barcode, extraction and validation and receives only exact prepared pixels/metadata.
- Station and worker factories split; station validation/import path does not load OCR/ZXing runtime.
- Existing `InspectionPipeline` retained as synchronous compatibility façade over the two boundaries.
- Existing local event IDs supplied by diagnostics remain accepted by the local façade; distributed contracts require UUID.

## TESTED

- Contract tests: 20 passed.
- Boundary tests: 6 passed.
- Existing focused POC/config/ranking/schema regressions: 25 passed.
- Full non-runtime suite: 161 passed, 3 runtime tests deselected.
- Python compileall: passed.
- Git diff whitespace check: passed; only line-ending conversion warnings were emitted by Git.
- Station import isolation was exercised in a clean subprocess.
- Worker camera-runtime isolation was exercised in a clean subprocess.

## RUNTIME VERIFIED

Nothing new is claimed runtime-verified on GX10 in Checkpoint 2A.

Historical RTSP/OCR evidence remains historical and was not rerun on this Windows host.

## NOT VERIFIED

- GX10/ARM64 execution of the new preparation/processor boundaries.
- Real RTSP station process; no station entrypoint exists until later checkpoints.
- Real PP-OCRv6/ZXing inference through the new worker boundary.
- MinIO, RabbitMQ, spool durability, restart recovery, publish confirm, manual ACK, retry or DLQ; these are not part of 2A.
- Strict single inference execution is intentionally not claimed.

## COMMANDS ACTUALLY RUN

```text
python -m pip install "pytest>=8.0"
python -m pip install "numpy>=1.26" "opencv-python-headless>=4.8" "python-dotenv>=1.0"
python -m pytest -q tests/test_schema.py tests/test_pipeline_contract.py tests/test_config_wiring.py tests/test_candidate_ranking.py
python -m pytest -q tests/test_phase2_contracts.py
python -m pytest -q tests/test_phase2_boundaries.py
python -m pytest -q -m "not runtime"
python -m compileall -q src scripts tests
git diff --check
```

RED evidence actually observed:

- Contracts test collection failed because `label_inspection.contracts` did not exist.
- Buffered-frame timestamp regression failed because the first implementation incorrectly required receipt after trigger.
- Boundary test collection failed because `label_inspection.station` did not exist.
- Boundary tests later had one expected failure because `build_station_preparer` did not yet exist.

## TEST RESULTS ACTUALLY OBSERVED

```text
Baseline focused suite: 25 passed in 1.55s
Contract GREEN: 20 passed in 0.06s
Boundary GREEN: 6 passed in 0.68s
Focused regressions after refactor: 25 passed in 0.41s
Full non-runtime suite: 161 passed, 3 deselected in 2.90s
compileall: exit 0
git diff --check: exit 0 with LF/CRLF warnings only
```

The first pytest installation command exceeded the tool's initial timeout, but a direct import immediately afterward confirmed `pytest 9.1.1` was installed. Base dependencies installed successfully. No OCR/GPU/broker/storage dependency was installed.

## KNOWN LIMITATIONS

- Job contract data is frozen in memory; immutable on-disk `job.json` begins in 2B.
- Terminal result variants exist as contracts/outcomes, but durable commit for every attempt begins in 2B.
- `PreparedInspection` is an in-memory boundary containing image objects; it is deliberately not a serialized distributed contract.
- The compatibility façade preserves old diagnostic IDs such as `INS-001`; production distributed identity must pass through the strict UUID contract.
- No performance claim is made from this checkpoint.
- The Nvidia/Customer/Our Part Number semantic blocker remains unchanged.

## CHECKPOINT DECISION

Checkpoint 2A acceptance criteria are satisfied by local automated evidence. Human review is required before starting Checkpoint 2B.
