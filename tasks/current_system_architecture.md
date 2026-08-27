# Vision-AI: Current System Architecture and Runtime Context

**Status:** Current runtime source of truth  
**Last reviewed:** 2026-08-24  
**Repository:** `vision-AI`  
**Acceptance branch:** `main`  
**Latest documented implementation:** `bb386dc Map split DGX label OCR fields`

This document describes what the repository does today, what the active GX10
runtime path is, how data moves through the system, what is measured, and what
is still missing before production deployment. It distinguishes a verified
runtime POC from a production-operated service.

## 1. Executive summary

The active system is a single-process Python inspection pipeline fed directly
by an Android IP camera over RTSP or HTTP. The camera is read continuously by a
dedicated acquisition thread into a bounded, timestamp-aware ring buffer. A
manual or timed trigger snapshots fresh frames from that buffer and sends them
through orientation normalization, Top-K frame selection, FixedROI crop and
quality gating, PP-OCRv6, ZXing DataMatrix decoding, DGX Spark field extraction,
validation, and event-scoped debug artifact persistence.

The active GX10 acceptance path is:

```text
Android IP Camera
  -> direct RTSP/HTTP URL
  -> OpenCV/FFmpeg RTSPCamera
  -> CameraAcquisition daemon thread
  -> bounded FrameBuffer
  -> fresh-frame trigger snapshot
  -> orientation normalization
  -> Top-K FrameSelector
  -> deterministic FixedROI
  -> crop / optional rectify / quality gate
  -> candidate ranking
  -> PP-OCRv6 DET+REC       (caller/warmup thread)
  -> ZXing DataMatrix       (background worker, overlaps OCR)
  -> DGX Spark FieldExtractor
  -> LabelValidator
  -> InspectionResult JSON
  -> selected_frame.jpg + label_crop.jpg + result.json
```

When `VISION_DETECTOR=yolo` is explicitly selected, the detector stage is
Ultralytics instead of FixedROI. The adapter is currently `EXPERIMENTAL` and
runtime acceptance is pending; startup records the exact checkpoint path,
SHA-256, class schema, configured/actual device, and inference thresholds.
Every fresh trigger also persists `detector_input.jpg` and
`detector_debug.json`, including misses, so detector acceptance can be audited
without treating OCR as having run.

## 4. End-to-end startup flow

The persistent runtime performs startup in this order:

```text
CLI/environment
  -> resolve source and ROI
  -> construct Settings override for this POC
  -> build_pipeline()
  -> validate configuration
  -> construct FixedROI
  -> construct DGX Spark FieldExtractor
  -> construct PP-OCRv6 adapter
  -> construct ZXing adapter
  -> OCR warmup
  -> ZXing prepare()
  -> create RTSPCamera
  -> create FrameBuffer
  -> start CameraAcquisition
  -> wait for connected + fresh frame
  -> SYSTEM READY
```

The manual POC intentionally forces these active components regardless of
general defaults:

```text
detector           = fixed-roi
ocr_engine         = ppocr_v6
ocr_backend        = transformers
ocr_version        = PP-OCRv6
barcode_engine     = zxing
extraction_profile = dgx_spark_label
required_fields    = DGX Spark field list
```

OCR warmup loads the cached PP-OCRv6 detection and recognition models and runs
a warmup inference. The model load/warmup time is recorded under startup and
is not included in per-inspection `ocr_ms`.

ZXing is prepared before camera readiness. `SYSTEM READY` is not emitted until
both runtimes are ready and the camera has produced a fresh buffered frame.

## 5. Camera and acquisition data flow

### 5.1 Source boundary

The Android IP Cam application supplies a direct URL such as:

```text
rtsp://PHONE_IP:PORT/PATH
http://PHONE_IP:PORT/PATH
```

The URL is passed to OpenCV/FFmpeg without a relay. HTTP/MJPEG support depends
on the OpenCV/FFmpeg build on GX10.

### 5.2 RTSPCamera

`RTSPCamera` owns the native `VideoCapture` object and provides:

- Lazy OpenCV import.
- FFmpeg backend selection where available.
- Open/read timeout hints.
- Small capture buffer request.
- Reconnect/backoff after failed reads.
- Connection and freshness health.
- Frame IDs and wall/monotonic timestamps.
- Credential masking in reported source values.
- Serialization of native `capture.read()` and `capture.release()`.

If `close()` arrives while a native read is active, release is deferred until
the read lock is free. `wait_closed()` lets the process shutdown path wait for
that native release to finish. This was added after the FFmpeg/OpenCV demuxer
assertion observed during earlier shutdown races.

### 5.3 CameraAcquisition

The acquisition thread repeatedly calls `camera.read()` and appends successful
`FramePacket` values to the buffer. The buffer is bounded, so old frames are
discarded automatically. A camera read failure is recorded in health and the
camera can reconnect through `RTSPCamera`.

### 5.4 FrameBuffer

The buffer has two independent protections:

```text
max_size  -> memory bound / newest-frame retention
window_ms -> time freshness bound
```

At trigger time, only frames inside the freshness window are handed to the
inspection pipeline. A successful connection with no fresh frame is not enough
to perform an inspection.

## 6. Trigger and inspection data flow

For each manual or timed trigger:

```text
trigger event
  -> create event_id = INS-XXXXXXXXXXXX
  -> snapshot buffer
  -> discard stale packets
  -> record camera/trigger telemetry
  -> rotate fresh packets if configured
  -> InspectionPipeline.inspect_packets()
```

### 6.1 Orientation

Orientation is normalized before FixedROI and OCR:

```text
raw camera frame
  -> rotate 0/90/180/270 clockwise
  -> normalized frame used by detector, crop, OCR, ZXing
```

The saved `selected_frame.jpg` is the oriented frame passed to the AI path.
Therefore it is the correct artifact to use when calibrating ROI.

### 6.2 Top-K selection

The selector filters stale packets, computes bounded preview-based quality
scores, and returns up to `top_k` packets. The default is three. Ranking uses a
combination of:

- Exposure/brightness.
- Sharpness.
- Freshness.

The pipeline does not run OCR on all Top-K packets. It builds candidates from
the selected packets, then runs OCR once on the best usable candidate.

### 6.3 Detection and crop

The default detector is FixedROI. It creates exactly one candidate from the
configured normalized or absolute coordinates. The trained Ultralytics/YOLO
adapter is also wired as an explicit opt-in and emits the same
`LabelCandidate` contract; it requires the detector model and device settings.
The candidate is cropped with the configured padding ratio. Perspective
rectification is available at the pipeline boundary, but a FixedROI candidate
normally has no corner geometry, so that path is effectively a deterministic
crop.

### 6.4 Quality gate

The crop is checked before inference. If no candidate passes quality, the event
returns `REVIEW` with a quality reason and OCR/ZXing remain `NOT_RUN`.

The current quality dimensions are:

```text
minimum width/height
sharpness
mean brightness
underexposed ratio
overexposed ratio
glare ratio
```

This is an intentional early gate: bad image quality should not be reported as
an OCR or field-extraction failure.

### 6.5 Candidate ranking

Every prepared candidate receives a weighted score using detection confidence,
sharpness, exposure, area, freshness, glare, and validity. The highest-scoring
quality-passing candidate is sent to OCR and ZXing.

### 6.6 OCR and barcode execution order

The current implementation overlaps OCR and barcode while preserving the OCR
runtime's caller-thread affinity:

```text
best crop
  -> submit ZXing decode() to one background worker
  -> PP-OCRv6 recognize() inline on the caller/warmup thread
  -> join the ZXing result
  -> field extraction
  -> validation
```

OCR and barcode do not share decoded data. ZXing remains an independent barcode
signal, which is important because a DataMatrix may be readable even when text
OCR or field extraction is incomplete. The executor is per inspection, but the
resident OCR and ZXing objects are reused and are not loaded again.

### 6.7 Business field extraction

The active profile is `dgx_spark_label` and contains:

```text
customer_part_number
so_number
our_part_number
quantity
net_weight
gross_weight
carton_number
```

Current label mappings include:

| Business field | Current label patterns |
|---|---|
| `customer_part_number` | `Customer Part Number`, `Customer P/N`, `CPN`, `Nvidia P/N` |
| `so_number` | `S/O NO.`, `Sales Order` |
| `our_part_number` | `OUR PART NO.`, `OUR P/N` |
| `quantity` | `QTY`, `Q'TY`, `QUANTITY` |
| `net_weight` | `N.W.`, `NET WEIGHT` |
| `gross_weight` | `G.W.`, `GROSS WEIGHT` |
| `carton_number` | `C/NO.`, `CARTON NO.`, `CTN` |

The DGX profile can join adjacent OCR lines. For example:

```text
Q'TY:
2
```

becomes `quantity = 2`. The same applies to the weight and carton-number
patterns. The extractor preserves confidence, source, and evidence line text.

`Carton ID` and `C/NO.` are treated as distinct label semantics. The current
`carton_number` mapping intentionally prefers the explicit carton-number form
`C/NO.` rather than silently conflating it with `Carton ID`.

### 6.8 Validation

Validation combines:

- Required-field presence.
- Minimum field confidence.
- OCR runtime state.
- Barcode result when barcode is configured as required.
- Quality result.

The business outcome is normally one of:

```text
PASS   -> required information is present and valid
REVIEW -> process ran but evidence is incomplete/uncertain
FAIL   -> validation/business rule failure
ERROR  -> runtime or pipeline failure
```

The process summary status `COMPLETED` is separate from the per-event business
status.

## 7. Output contracts and artifacts

### 7.1 Per-event result

Each event JSON contains, depending on the path:

```text
status
event_id
camera_id
source (credentials masked)
telemetry
selected_frame_id
selected_frame_timestamp
label_bbox
crop_bbox
crop_score
quality
ocr
barcode
fields
validation
timings
artifacts
```

### 7.2 Timing contract

The pipeline records:

```text
frame_selection_ms
detection_ms
crop_rectify_ms
quality_ms
candidate_ranking_ms
ocr_ms
barcode_ms
parallel_inference_ms
field_extraction_ms
validation_ms
total_ms
```

The manual acceptance script additionally records wall time including artifact
writing and reports OCR, barcode, parallel-inference, and total-inspection
p50/p95 after the configured warmup period. Total-inspection metrics include
all executed inference attempts; PASS-only latency remains a separate view.

Model loading and warmup belong to startup telemetry and must not be used as a
steady-state OCR latency measurement.

### 7.3 Debug artifacts

Each completed event must persist:

```text
<debug-root>/<event_id>/selected_frame.jpg
<debug-root>/<event_id>/label_crop.jpg
<debug-root>/<event_id>/result.json
```

The artifact directory is the primary evidence bundle for debugging ROI,
orientation, OCR lines, field mapping, barcode position, and validation.
`label_crop.jpg` is encoded from a snapshot of the exact prepared crop object
passed to OCR/ZXing; the manual script does not reconstruct it from the bbox.

## 8. Configuration and environment

`src/label_inspection/config.py` loads environment values into an immutable
`Settings` object. The module uses `python-dotenv` when available and does not
initialize CUDA or model runtimes merely by importing configuration.

Important active settings:

| Variable | Meaning | Current/default behavior |
|---|---|---|
| `VISION_RTSP_URL` | Direct phone-camera URL | CLI source takes precedence |
| `VISION_CAMERA_ID` | Camera identity | `PHONE-01` |
| `VISION_BUFFER_SIZE` | Ring-buffer capacity | `8` |
| `VISION_BUFFER_WINDOW_MS` | Freshness window | `800` |
| `VISION_MAX_FRAME_AGE_MS` | Selector/health freshness limit | `1000` |
| `VISION_RTSP_OPEN_TIMEOUT_MS` | OpenCV open timeout hint | `5000` |
| `VISION_RTSP_READ_TIMEOUT_MS` | OpenCV read timeout hint | `2000` |
| `VISION_CAMERA_ROTATE_DEG` | Clockwise orientation normalization | `0` |
| `VISION_LABEL_ROI` | FixedROI coordinates | Required for FixedROI |
| `VISION_ROI_NORMALIZED` | ROI coordinate mode | `true` |
| `VISION_TOP_K` | Maximum candidate frames | `3` |
| `VISION_BBOX_PADDING_RATIO` | Crop padding | `0.05` |
| `VISION_OCR_DEVICE` | General OCR device default | `cpu` |
| `VISION_OCR_ENGINE` | General OCR engine default | `ppocr` |
| `VISION_OCR_BACKEND` | OCR backend | `transformers` |
| `VISION_OCR_VERSION` | OCR version | `PP-OCRv6` |
| `VISION_BARCODE_ENGINE` | Barcode backend | `zxing` |
| `VISION_EXTRACTION_PROFILE` | Generic profile selection | `default`; manual POC forces DGX |
| `VISION_REQUIRED_FIELDS` | Generic required fields | default `sku`; manual POC forces DGX list |
| `VISION_BARCODE_REQUIRED` | Whether validator requires barcode | `false` unless enabled |
| `VISION_OCR_CONFIDENCE` | Minimum field confidence | `0.70` |
| `VISION_QUALITY_MAX_OVEREXPOSED_RATIO` | Quality threshold | Environment-calibrated |
| `VISION_QUALITY_MAX_GLARE_RATIO` | Quality threshold | Environment-calibrated |

The manual POC accepts CLI values for source, ROI, rotation, device, trigger
count, and debug root. It then overrides the general settings to the active
GX10 stack described above.

## 9. Dependencies and external boundaries

### 9.1 Runtime dependencies

The active path depends on:

- Python 3.
- NumPy.
- OpenCV headless with FFmpeg support.
- PaddlePaddle/PaddleOCR/PaddleX runtime compatible with GX10.
- Cached PP-OCRv6 DET/REC model files.
- ZXing-C++ Python runtime/binding.
- `python-dotenv` when `.env` loading is desired.

The model cache is local to the GX10 user environment after first download. The
application does not call a remote OCR service.

### 9.2 External services

The only active network data source is the Android camera stream. There is no
active database, message broker, object store, metrics backend, tracing
collector, or HTTP API service in the current POC process.

### 9.3 Deferred code paths

The repository contains adapters for TensorRT, Ultralytics, contour detection,
and generic PaddleOCR. Their presence does not mean they are part of the
acceptance runtime. The GX10 POC intentionally uses PP-OCRv6 Transformers,
FixedROI, and ZXing.

## 10. Current latency and throughput evidence

The latest GX10 runtime acceptance run completed 10 triggers in one process
with resident model reuse. The observed values were approximately:

```text
OCR warmup:             3.56 s (startup only)
OCR p50:              183 ms
OCR p95:              296 ms
pipeline total p50:  255 ms
pipeline total p95:  369 ms
total inspection p50: 264 ms
total inspection p95: 379 ms
```

These values describe the tested camera, crop, image quality, device, and
configuration. They are not a universal production SLA.

Because OCR and barcode overlap, the steady-state critical path is approximately:

```text
selection + detection + crop + quality + ranking
  + max(OCR, barcode)
  + extraction + validation
  + artifact write (for total inspection wall time)
```

This is the intended wall-time shape, not a measured GX10 speedup claim. Native
runtime contention and decoder behavior must still be benchmarked on GX10.

## 11. Traceability, evaluation, and monitoring status

### 11.1 Traceability

The system has an event-level correlation identifier:

```text
event_id = INS-XXXXXXXXXXXX
```

It links the per-event JSON and both image artifacts. `camera_id` and
`selected_frame_id` provide additional local correlation.

This is not distributed tracing. The current system does not have:

- OpenTelemetry trace IDs.
- Per-stage spans.
- Trace propagation.
- Trace export to Jaeger, Tempo, or an OTel collector.
- Event IDs in every structured application log.

Current status:

```text
event correlation ID: present
distributed trace ID: absent
stage spans:          absent
```

### 11.2 Evaluation

The repository now contains a reusable evaluator at
`src/label_inspection/evaluation/` and the CLI
`scripts/evaluate_dataset.py`. It executes the same FixedROI, quality,
PP-OCRv6, ZXing, FieldExtractor, and LabelValidator path used by the runtime.
PP-OCRv6 and ZXing are initialized once per evaluation process and reused for
all samples.

The evaluator emits field exact-match/missing/wrong/false-extraction metrics,
strict DataMatrix payload/format/validity metrics, business
PASS/REVIEW/FAIL/ERROR and required-output-aware false-pass metrics,
quality/ROI/failure/condition metrics, and latency mean/p50/p95/p99/max.
Production accuracy is gated to target-role, human-verified, non-synthetic
records; runtime failures remain in its denominator. With zero eligible ground
truth the report uses `NOT_VERIFIED`, never a fabricated 0% or 100%.

Every run writes to an immutable `<output_root>/<run_id>` directory and records
Git state, dataset/config fingerprints, ROI, quality thresholds, dependency
versions, and extractor semantics in `provenance.json`. Real-sample condition
rows cover lighting, glare, blur, distance, position, rotation, and occlusion;
the insufficient-sample threshold is evaluator configuration (default 5).

The DGX production extractor currently retains its historical `Nvidia P/N` to
`customer_part_number` alias. Evaluation marks this as
`KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION`, preserves the raw OCR
line, and does not present `customer_part_number` as production-verified until
the mapping is confirmed.

The current dataset is still not accuracy-verified: the target contains one
real DGX smoke image with `human_verified=false`. Synthetic DGX variants and
the generic shipping seed are reported in separate robustness datasets.

Still absent from the evaluation framework or target data:

- OCR character-level accuracy and representative human-verified samples.
- Production field mapping confirmation for NVIDIA P/N vs Customer Part No.
- Public dataset downloads requiring network/credentials.
- Production monitoring, distributed tracing, and alerting.

### 11.3 Monitoring

Per-event telemetry exists in JSON and camera health exists in memory:

```text
connected
stale
frames_received
last_frame_at
reconnect_count
last_error
```

There is no production monitoring plane yet:

- No Prometheus exporter.
- No Grafana dashboard.
- No OpenTelemetry metrics.
- No alerting for stale camera, OCR failure, barcode failure, p95 regression,
  or GPU memory pressure.
- No centralized log aggregation.
- No retention policy for artifacts.

The JSON output is useful for local acceptance, not a monitoring system.

## 12. Runtime invariants

The following rules must not be broken without revisiting the architecture:

1. Camera reads remain isolated in the acquisition worker; the controller must
   not call blocking native camera I/O directly.
2. The frame buffer remains bounded by size and freshness window.
3. Stale frames must not enter an acceptance inspection.
4. OCR and ZXing must not run before their startup readiness gates pass.
5. OCR model load/warmup must remain outside steady-state `ocr_ms`.
6. The resident OCR instance must be reused across triggers.
7. Orientation must be normalized before applying FixedROI and OCR.
8. ROI coordinates must be calibrated against the normalized selected frame.
9. Quality rejection must stop OCR/ZXing and remain distinguishable from an OCR
   runtime failure.
10. DataMatrix decoding must remain independent from text field extraction.
11. Field extraction must not invent values absent from OCR evidence.
12. Every event must have an event ID and an artifact evidence bundle.
13. Native OpenCV/FFmpeg release must not race an active native read.
14. A process-level `COMPLETED` result must not be interpreted as a business
   `PASS` result.
15. Any parallel OCR/barcode change must preserve timing attribution and prove
   decoder/model thread safety on the target device.

## 13. Important coupling and failure modes

| Coupling/failure | Current behavior | Operational implication |
|---|---|---|
| ROI ↔ orientation | ROI applies after rotation | Recalibrate ROI when rotation changes |
| Quality ↔ OCR/barcode | Quality gate runs first | Bad lighting produces `REVIEW` with inference not run |
| Crop ↔ OCR/ZXing | Both use same crop | Wrong crop affects both branches |
| Buffer window ↔ Wi-Fi jitter | Old frames are discarded | Increase window carefully; stale evidence is unsafe |
| Model cache ↔ startup | Missing cache causes startup download/load | First startup is slower and depends on model availability |
| Native read ↔ shutdown | Release can be deferred | `wait_closed()` must complete before process exit |
| Third-party logs ↔ JSON pipe | Paddle/runtime logs may precede JSON on stdout | Direct `jq` piping can fail unless logs are filtered/captured |
| `COMPLETED` ↔ business status | Process and label statuses are separate | Always inspect `fields`, `barcode`, and `validation` |
| `Carton ID` ↔ `C/NO.` | They may represent different identifiers | Current `carton_number` maps explicit carton-number labels |

## 14. Production-readiness assessment

### Ready/proven at POC level

- Direct Android IP camera acquisition on GX10.
- Ring buffer and fresh-frame selection.
- Persistent same-process trigger loop.
- PP-OCRv6 resident loading and inference.
- Real ZXing DataMatrix decode.
- FixedROI crop and orientation path.
- DGX Spark field extraction for the tested label format.
- Event artifacts and stage timings.
- 10-trigger runtime benchmark.
- Safe normal shutdown after the FFmpeg release race fix.

### Not yet production-complete

- Production service lifecycle and supervisor.
- Health/readiness endpoint.
- Metrics and alerting backend.
- Distributed tracing.
- Ground-truth dataset evaluation.
- Soak test and reconnect/disconnect endurance test.
- Load/backpressure policy for concurrent triggers.
- Explicit idempotency/event admission policy.
- Schema versioning and result compatibility contract.
- Artifact retention and storage policy.
- Secret/authentication/network hardening for camera deployment.
- Accuracy SLA and business acceptance thresholds.
- Parallel OCR/ZXing benchmark decision.

The correct current classification is:

```text
Real, validated GX10 runtime POC
with production-oriented module boundaries,
but not yet a fully operated production service.
```

## 15. Recommended next engineering gates

The next work should be evidence-driven and staged:

1. Freeze the current runtime result schema and add a schema version.
2. Create a versioned DGX label dataset with ground-truth JSON.
3. Add field-level and barcode-level evaluation reports.
4. Add structured logs containing `event_id`, `camera_id`, and stage outcome.
5. Add OpenTelemetry spans around acquisition, selection, quality, OCR, barcode,
   extraction, validation, and artifact write.
6. Export Prometheus-compatible counters/histograms and camera health gauges.
7. Run soak tests covering Wi-Fi jitter, camera disconnect, reconnect, stale
   frames, and repeated shutdown/startup.
8. Benchmark parallel OCR/ZXing only after correctness and decoder thread
   safety are proven.
9. Add a supervisor/restart policy and define deployment resource limits.
10. Define the production PASS/REVIEW/FAIL policy with the business owner.

Until those gates are complete, latency numbers should be reported as target
runtime evidence rather than a production SLA.



There is no MediaMTX, Redis, GLM-OCR, custom YOLO, TensorRT, or database in
this active path.

## 2. Scope and runtime modes

### 2.1 Active GX10 POC mode

The production-like runtime test is [`scripts/manual_rtsp_inspection.py`](../scripts/manual_rtsp_inspection.py).
It is a persistent process designed to prove:

- OCR and ZXing readiness before the system becomes ready.
- Direct phone-camera connectivity.
- Continuous acquisition into a bounded ring buffer.
- Fresh-frame gating at trigger time.
- Same-process model reuse across multiple inspections.
- Top-K selection and FixedROI processing.
- Real PP-OCRv6 inference.
- Real ZXing DataMatrix decoding.
- DGX Spark business-field extraction.
- One artifact directory per inspection event.
- p50/p95 latency after warmup.
- Safe shutdown around native OpenCV/FFmpeg release.

### 2.2 Other repository entrypoints

These are useful tools, but are not the persistent acceptance runtime:

| Entrypoint | Purpose | Active acceptance role |
|---|---|---|
| `scripts/manual_rtsp_inspection.py` | Persistent direct RTSP/HTTP loop with warmup, trigger loop, artifacts and benchmark | **Primary** |
| `scripts/run_real_image_integration.py` | Real image path and one-image benchmark | Pre-RTSP image validation |
| `scripts/run_real_rtsp_integration.py` | One-shot RTSP acquisition and inspection | Diagnostic/legacy integration path |
| `scripts/inspect_rtsp.py` | Capture a bounded RTSP window and inspect it | Diagnostic |
| `scripts/camera_smoke.py` | Camera connectivity/read/health check only | Diagnostic |
| `scripts/inspect_image.py` | Single image inspection | Diagnostic |
| `scripts/replay_video.py` | Video capture/replay into the same inspection pipeline | Offline diagnostic |
| `scripts/check_runtime.py` | Dependency/runtime readiness diagnostic | Diagnostic |
| `scripts/test_ppocr_v6.py` | OCR-specific runtime check | Diagnostic |
| `scripts/test_zxing_runtime.py` | ZXing-specific runtime check | Diagnostic |
| `scripts/build_tensorrt_engine.py` | TensorRT engine tooling | Deferred, not active in POC |

## 3. Process and component architecture

### 3.1 Process model

The acceptance application is one Python process containing:

1. Main/controller thread.
2. One camera acquisition daemon thread.
3. Resident PP-OCRv6 DET and REC model objects.
4. Resident ZXing decoder/runtime.
5. Synchronous inspection work executed by the controller after each trigger.

The camera thread and inspection controller are intentionally separated. The
controller never performs a potentially blocking camera read. It only waits
for readiness and reads snapshots from the buffer.

The current OCR and barcode stages overlap, with OCR kept on its caller/warmup
thread for native GPU runtime safety:

```text
best_label_crop
  -> submit ZXing decode() to one background worker
  -> OCR recognize() inline on the caller/warmup thread
  -> join the ZXing result
  -> field extraction
  -> validation
```

The same crop is used by both decoders. This concurrency path is implemented
and covered by local regression tests, but its native-library behavior and
speedup are not runtime-verified on GX10 yet.

### 3.2 Main module map

```text
src/label_inspection/
├── app.py
│   └── build_pipeline(): configuration-backed component factory
├── config.py
│   └── Settings, .env/environment parsing and validation
├── schemas.py
│   └── FramePacket, OCRLine, InspectionResult, field/barcode/quality contracts
├── artifacts.py
│   └── event-scoped image and result JSON persistence
├── timing.py
│   └── stage timing helpers
├── camera/
│   ├── base.py
│   │   └── CameraSource protocol
│   ├── rtsp.py
│   │   └── OpenCV/FFmpeg camera source, reconnect, health, safe close
│   ├── acquisition.py
│   │   └── camera reader thread and bounded-stop orchestration
│   ├── frame_buffer.py
│   │   └── bounded timestamp-aware ring buffer
│   ├── selector.py
│   │   └── fresh-frame and Top-K ranking
│   └── security.py
│       └── source resolution and credential masking
├── detection/
│   ├── fixed_roi.py          active deterministic detector
│   ├── contour.py            experimental detector
│   └── ultralytics_detector.py supported YOLO detector adapter
├── preprocessing/
│   ├── orientation.py
│   ├── crop.py
│   ├── rectify.py
│   └── quality.py
├── ocr/
│   ├── ppocr_v6.py           active PP-OCRv6 Transformers adapter
│   ├── ppocr.py              alternate PaddleOCR adapter
│   └── tensorrt_ocr.py       deferred TensorRT adapter
├── barcode/
│   ├── base.py
│   └── zxing.py              active ZXing decoder
├── extraction/
│   ├── fields.py
│   └── profiles.py           default and DGX Spark profiles
├── validation/
│   └── rules.py
└── pipeline/
    ├── inspection.py         end-to-end synchronous inspection stages
    └── ranking.py            candidate quality score
```
