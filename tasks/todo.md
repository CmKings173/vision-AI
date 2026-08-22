# V2 OCR/Label Inspection Task List

## Phase 0 — Foundation

- [x] T1: Create isolated V2 package metadata and README.
- [x] T2: Define structured inspection schemas and JSON contract.

## Phase 1 — Camera

- [x] T3: Implement camera abstraction and RTSP reconnect reader.
- [x] T4: Implement bounded timestamped ring buffer.
- [x] T5: Implement top-K frame selector and replay/smoke scripts.

## Phase 2 — Label preparation

- [x] T6: Implement FixedROI and contour detector interfaces.
- [x] T7: Extract/refactor Ultralytics detector adapter for `shipping_label.pt`.
- [x] T8: Implement padding, crop, conditional perspective correction, and quality checks.

## Phase 3 — OCR/barcode V1

- [x] T9: Implement ZXing-C++ barcode adapter.
- [x] T10: Implement resident PP-OCR adapter.
- [x] T11: Implement SKU/LOT FieldExtractor.
- [x] T12: Implement deterministic validation and result statuses.

## Phase 3A — GX10 native TensorRT backend

- [x] G1: Add resident TensorRT engine runner using TensorRT + cuda-python.
- [x] G2: Add PP-OCR DB/CTC preprocessing and postprocessing adapter.
- [x] G3: Add TensorRT engine build script, config wiring, runtime checks, and contract tests.
- [ ] G4: Convert PP-OCR ONNX models and build/test target-specific engines on GX10.

## Phase 4 — Fallback

- [ ] T13: Implement alternate preprocessing and multi-frame retry.
- [ ] T14: Implement optional GLM-OCR sidecar/client adapter.
- [ ] T15: Add model health/version reporting.

## V1 Hardening Pass — GX10 code readiness

- [x] H1: Rank all Top-K label crops before a single OCR/barcode call.
- [x] H2: Make FixedROI explicit and fail fast for missing/invalid ROI.
- [x] H3: Harden RTSP count, timeout hints, stale-frame filtering, health, and shutdown.
- [x] H4: Mask RTSP credentials in all application-owned output.
- [x] H5: Keep OCR/barcode exceptions inside structured result boundaries.
- [x] H6: Wire engine, confidence, and framework-specific device configuration.
- [x] H7: Measure crop sharpness/exposure/glare and test actual OpenCV images.
- [x] H8: Separate unit/integration/runtime verification and add runtime readiness command.
- [x] H9: Normalize ZXing `valid`/Position output and enforce JSON-safe results.
- [x] H10: Add explicit NOT_RUN/SUCCESS/FAILED semantics for OCR, barcode, and quality.
- [x] H11: Isolate blocking RTSP acquisition and wire stale-safe health/source handling.
- [x] H12: Harden replay/smoke exit codes and actual runtime import/GPU checks.
- [x] H13: Downsample frame preselection; benchmark eight 4K frames before final report.
- [x] H14: Clean detector scope, dead config, install docs, and commercial license blocker.

## Phase 5 — Queue/worker

- [ ] T16: Define broker interface and in-memory test broker.
- [ ] T17: Implement Redis Streams ACK/retry/idempotency/DLQ.
- [ ] T18: Implement one in-process Vision Worker for the complete inspection flow.

## Phase 6 — Model/deployment

- [ ] T19: Train/integrate custom `shipping_label` detector.
- [ ] T20: Benchmark CPU/GPU deployment profiles.
- [ ] T21: Document Docker/runtime/license requirements.

## Checkpoints

- [x] Foundation: schema tests pass and old repository remains unchanged.
- [ ] Camera: RTSP/video replay works and buffer is bounded.
- [ ] Local V1: image inspection returns OCR/barcode/validation JSON with timings.
- [ ] Fallback: failed PP-OCR cases degrade to REVIEW safely.
- [ ] Worker: Redis job processing is retryable and never stores raw frames.
- [ ] Complete: acceptance dataset meets accuracy/latency targets.

## Current status

- T1–T12 plus V1 hardening are code-complete and locally unit/integration-tested.
- G1–G3 are code-complete; G4 remains target-runtime work because the repository does not contain PP-OCR ONNX/engine model artifacts.
- Runtime verification remains blocked by the current host environment and is reported as SKIP/FAIL, not PASS.
- Latest local verification: 60 unit + 11 integration passed; 3 external runtime tests skipped.
- Latest 8x4K selector benchmark (480 px preview): 762.925 ms → 31.430 ms median (24.27x).
- Deferred by request: GLM-OCR, Redis Streams, custom YOLO training/integration, and deployment hardening.
- Future: calibrate quality thresholds and candidate weights from real GX10/phone captures.
- Future: benchmark PP-OCR/ZXing and p50/p95 timings on GX10 before deciding on GLM fallback.
- Blocker: resolve Ultralytics licensing/commercial-use terms before enabling that adapter in a commercial deployment.
- Target project directory: `/Users/boss/Projects/vision-AI`.
- Legacy source directory: `/Users/boss/Projects/vison-AI-server` (untouched).
