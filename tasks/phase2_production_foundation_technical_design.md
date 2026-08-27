# Phase 2 — Production Foundation V1 Technical Design

## 1. Status and authority

Status: **APPROVED FOR IMPLEMENTATION**

Implementation status (2026-08-25): **Checkpoints 2A through 2F are implemented
and tested locally. GX10 runtime acceptance with real MinIO, RabbitMQ,
PP-OCRv6, ZXing and RTSP remains pending.** Deterministic fakes do not count as
runtime verification; see `tasks/phase2_implementation_report.md`.

This document is the target architecture for Phase 2. The repository audit in `tasks/phase2_production_foundation_audit.md` is the source of truth for the code that currently exists. The execution plan and progress checklist are:

- `tasks/phase2_production_foundation_plan.md`
- `tasks/phase2_production_foundation_todo.md`

The pre-2A human review gate is approved. Implementation proceeds checkpoint by checkpoint and must stop after each checkpoint for an evidence-based report.

## 2. Scope

Phase 2 introduces two coarse-grained application services:

1. `station-service`
2. `inference-worker`

Infrastructure services:

- MinIO
- RabbitMQ

Each application service remains modular. Camera, ROI, quality, OCR, barcode, extraction and validation are modules, not separate microservices.

Phase 2 preserves the current RTSP, Top-K, orientation, FixedROI, PP-OCRv6, ZXing, FieldExtractor and LabelValidator behavior unless a compatibility refactor is required.

Out of scope:

- YOLO or another custom detector
- VLM/GLM fallback
- TensorRT optimization
- PostgreSQL, Redis, Kafka or Kubernetes
- ERP integration
- Industrial-camera migration
- OCR/ZXing parallelization
- Business remapping of Nvidia P/N, Customer Part Number or Our Part Number
- Automatic deletion of committed spool data

## 3. Target flow

### 3.1 Quality-pass inference path

```text
RTSP Camera
→ CameraAcquisition background thread
→ bounded RingBuffer
→ manual ENTER trigger
→ UUID event_id + trigger_id
→ snapshot recent frames
→ freshness check
→ Top-K selection
→ orientation normalization
→ FixedROI
→ exact label crop
→ quality PASS
→ immutable InspectionJob
→ atomic Local Spool commit
→ background Outbox Dispatcher
→ MinIO artifacts durable
→ RabbitMQ confirmed publish
→ inference-worker
→ contract/idempotency validation
→ exact crop download + SHA-256 verification
→ PP-OCRv6
→ ZXing
→ FieldExtractor
→ LabelValidator
→ durable InspectionResult in MinIO
→ RabbitMQ ACK
```

### 3.2 Quality-rejected terminal path

Quality rejection is a completed inspection attempt, not a technical error and not a worker job.

```text
quality rejected
→ processing_status = COMPLETED
→ business_status = REVIEW
→ inference_executed = false
→ reasons include QUALITY_REJECTED
→ terminal InspectionResult
→ atomic Local Spool commit
→ background upload of result/artifacts to MinIO
→ done; no RabbitMQ inference message
```

### 3.3 Preparation technical-error terminal path

Errors such as stale/disconnected camera, no candidate, invalid ROI, crop failure or artifact-preparation failure are technical terminal results:

```text
processing_status = ERROR
business_status = null
error = {code, stage, message, retryable, attempt, safe_details?}
→ atomic Local Spool commit whenever local disk is operational
→ background upload of result to MinIO
→ done; no RabbitMQ inference message
```

They never become business `FAIL`.

## 4. Durability guarantee

Every accepted manual trigger creates an inspection attempt and receives an `event_id` before preparation begins.

Every attempt follows exactly one local-commit path:

1. Preparation technical error → terminal `result.json`.
2. Quality rejected → terminal `result.json` and available artifacts.
3. Quality pass → immutable `job.json` and required artifacts.

Before atomic local commit, data may still be transient. After atomic local commit, the attempt must not silently disappear because MinIO or RabbitMQ is unavailable, the worker crashes, or station restarts.

If local disk or atomic commit fails, the station must report a structured `ARTIFACT_WRITE_ERROR` or `SPOOL_COMMIT_ERROR`. It must not claim the attempt is durable.

## 5. Camera and thread model

`CameraAcquisition` continues reading independently in a background thread:

```text
RTSP read → FramePacket → timestamps → bounded RingBuffer → continue reading
```

The camera thread never performs OCR, barcode, MinIO, RabbitMQ or business validation.

The RingBuffer remains bounded and thread-safe. Snapshot locking is limited to copying frame references/data. Top-K, ROI, quality, storage and inference run outside the RingBuffer lock. Existing reconnect, timeout, health and shutdown hardening must be preserved.

## 6. Identity and time

### 6.1 Distributed identity

V1 uses stdlib UUIDv4 for `event_id` and `trigger_id`. UUID version is an implementation detail, not business semantics.

The same `event_id` propagates through:

- structured logs
- spool directory and manifests
- MinIO object keys
- RabbitMQ `message_id` and `correlation_id`
- InspectionJob and InspectionResult
- future persistence/integration boundaries

Database auto-increment IDs are not distributed event identity.

### 6.2 Absolute timestamps

Persistent absolute timestamps use integer Unix Epoch milliseconds and the `_ms` suffix.

Required current RTSP semantics:

- `received_at_ms`: when the station/GX10 receives or records the frame.
- `source_timestamp_ms`: optional source/camera timestamp only when available and trustworthy.
- `captured_at_ms`: not used unless actual physical capture semantics are valid and documented.

Other lifecycle examples are `triggered_at_ms`, `prepared_at_ms`, `published_at_ms`, `processing_started_at_ms` and `completed_at_ms`.

Durations use monotonic time internally, preferably `monotonic_ns()`. Raw monotonic timestamps are never serialized; only computed values such as `ocr_ms` and `worker_total_ms` are persisted.

## 7. Service ownership

### 7.1 Station owns

- RTSP and CameraAcquisition
- RingBuffer
- manual trigger and attempt creation
- freshness and Top-K selection
- orientation normalization
- FixedROI and exact crop
- quality gate
- Local Spool
- Outbox Dispatcher

The station answers: **Which exact image should this attempt analyze?**

After Phase 2 completion, station must not load or call OCR/ZXing.

### 7.2 Worker owns

- InspectionJob boundary validation
- MinIO artifact retrieval
- checksum verification
- PP-OCRv6 and ZXing
- FieldExtractor and LabelValidator
- terminal InspectionResult construction and persistence

The worker answers: **What data is present in this exact prepared image?**

The worker never accesses RTSP, camera or RingBuffer; it never reselects a frame, reruns FixedROI or reconstructs a crop.

## 8. Exact-crop invariant

Station preparation produces the exact label pixels consumed by worker inference. It persists:

- `selected_frame.jpg`
- `label_crop.png`

The crop remains lossless PNG in V1. It must never be reconstructed later from bbox metadata. The SHA-256 in `job.json` refers to the exact bytes uploaded to MinIO and downloaded by the worker.

## 9. Local Spool and immutable job

The spool root is configurable, for example `/var/lib/vision/spool/`.

Quality-pass layout:

```text
<event_id>/
  selected_frame.jpg
  label_crop.png
  job.json
  state.json
```

Quality-rejected layout:

```text
<event_id>/
  selected_frame.jpg
  label_crop.png       # optional if generated
  result.json
  state.json
```

Early technical-error layout:

```text
<event_id>/
  result.json
  state.json
```

Atomic commit uses a temporary directory under the same parent/filesystem:

```text
.tmp_<event_id>
→ write all required files
→ flush/fsync where supported
→ validate files and checksums
→ atomic rename
→ <event_id>
```

Only the final directory is dispatchable. All resolved paths must remain inside spool root; reject traversal, separators in IDs, drive escape and symlink escape.

For quality PASS, the complete InspectionJob is created once before local commit. After commit, `job.json` is immutable. Dispatcher reads and publishes the exact frozen payload; it never rebuilds or mutates the business job after restart or config/code changes.

## 10. Contracts

Cross-process versions are explicit, beginning with:

- `inspection-job.v1`
- `inspection-result.v1`

Unsupported versions are rejected without guessing/coercing an unknown major version. Existing local POC schemas remain available through compatibility wrappers/facades.

InspectionJob includes at least:

- schema version
- event and trigger UUIDs
- station and camera IDs
- `triggered_at_ms`, `received_at_ms`, `prepared_at_ms`, `created_at_ms`
- optional trustworthy `source_timestamp_ms`
- selection/orientation/locator/quality metadata
- artifact references
- profile/version/provenance

Artifact reference includes bucket, key, SHA-256, content type and optional size. Jobs contain no image/base64 bytes or camera, MinIO or RabbitMQ credentials.

Structured errors include `code`, `stage`, `message`, `retryable`, `attempt` and optional safe details.

## 11. Status domains and ownership

Three domains remain separate.

Processing:

```text
CREATED → CAPTURED → PREPARED → QUEUED → PROCESSING → COMPLETED
                                                 └→ ERROR
```

Business:

```text
null | PASS | REVIEW | FAIL
```

Delivery:

```text
LOCAL_ONLY → ARTIFACTS_READY → JOB_PUBLISHED (inference) or
LOCAL_ONLY → ARTIFACTS_READY → TERMINAL_RESULT_DURABLE (station terminal)
```

Ownership:

| Owner | Transitions/data |
|---|---|
| Station | `CREATED`, `CAPTURED`, `PREPARED` |
| Station terminal path | `COMPLETED + REVIEW` for quality rejection; `ERROR + null` for preparation error |
| Local Spool/Dispatcher | `LOCAL_ONLY`, `ARTIFACTS_READY`, `JOB_PUBLISHED`, `TERMINAL_RESULT_DURABLE` |
| Confirmed publisher | May move inference processing state to `QUEUED` |
| Worker | `PROCESSING`, `COMPLETED`, technical `ERROR` |
| Validator | Business `PASS`, `REVIEW`, `FAIL` |

`state.json` is primarily station delivery/recovery state. `result.json` is terminal processing/business truth. Rabbit headers may carry transport metadata but are not an independent business-status source.

## 12. MinIO and Outbox Dispatcher

V1 bucket: `vision-inspections`.

Deterministic object layout is station/date/event based and separates source, metadata and result objects. Credential values exist only in environment/config.

Uploads are checksum-idempotent:

- same key and same checksum → success
- same key and different checksum → conflict/error

For inference attempts, ordering is strict:

```text
local commit
→ upload required artifacts and frozen job metadata
→ verify durable
→ ARTIFACTS_READY
→ confirmed RabbitMQ publish
```

For terminal station results, dispatcher uploads result/artifacts and finishes delivery without publishing an inference message.

Network failure never blocks CameraAcquisition. Trigger processing returns after successful local durable commit; dispatcher retries in the background.

## 13. RabbitMQ

V1 topology:

- Exchange: `vision.inspection.x`
- Routing key: `inspection.process`
- Queue: `vision.inspection.q`
- Final DLQ: `vision.inspection.dlq`

Required behavior:

- durable exchange and queue
- persistent messages
- publisher confirms
- manual ACK
- initial `prefetch_count = 1`
- `message_id = event_id`
- `correlation_id = event_id`

One quality-pass inspection equals one inference job. There are no OCR/barcode/extractor/validator queues.

Retry is bounded and configurable. Default schedule is 5, 30 and 120 seconds before final DLQ. Checkpoint 2D creates a topology compatible with this policy; checkpoint 2F completes retry routing/classification. DLQ is technical failure, never business `FAIL`.

## 14. Worker lifecycle and ACK invariant

Worker startup:

```text
config → MinIO → load PP-OCRv6 once → initialize ZXing
→ OCR warmup → RabbitMQ connection → WORKER READY → consume
```

Worker does not report ready until model and dependencies are usable. Per job,
OCR stays on the caller/warmup thread while ZXing runs on one background worker;
extraction and validation wait for both results.

Hard invariant:

```text
process → build result → persist and verify durable result → ACK
```

ACK before durable result is forbidden. A crash before ACK causes RabbitMQ redelivery.

## 15. Delivery guarantee and idempotency

V1 guarantees **at-least-once delivery plus logical idempotency of the durable result**.

Before inference, worker may detect an existing valid durable result for the same `event_id`. A valid duplicate is ACKed without rerunning inference. Existing result validation must include supported schema and matching event identity.

V1 does **not** claim exactly-once delivery or strict single execution of inference. Concurrent workers or crash races may execute inference more than once before one durable result becomes visible. The guarantee is that retries/redeliveries converge on one logically valid durable result without silent conflicting overwrite.

Stronger single-execution coordination is deferred and must not be claimed without evidence.

## 16. Backpressure and retention

Configurable safety guards:

- `max_pending_events`
- `max_pending_bytes`
- `min_free_disk_bytes`

When a safe limit is exceeded, station becomes degraded/not-ready and rejects new triggers. It never silently deletes evidence.

Retention/cleanup policy is not approved. V1 defaults to no automatic deletion of committed inspection artifacts.

## 17. Business semantics

Phase 2 reuses current PP-OCRv6, ZXing, FieldExtractor and LabelValidator semantics. It does not convert current string fields into a new numeric business schema, change required fields/confidence thresholds/barcode rules, add `nvidia_part_number`, or reinterpret existing mappings.

Known semantic blocker:

```text
Nvidia P/N ↔ Customer Part Number ↔ Our Part Number
```

Current behavior and profile/extractor provenance remain traceable. `customer_part_number` cannot be claimed production-correct until business confirms the mapping. This does not block infrastructure implementation.

## 18. Observability and performance

Structured lifecycle logs carry applicable identity, ownership and timing fields: event/trigger/station/camera IDs, component/stage, three status domains, attempt, retryability, safe error code, epoch-ms timestamp and duration.

Minimum persisted durations:

- `trigger_to_local_commit_ms`
- `spool_write_ms`
- `artifact_upload_ms`
- `local_commit_to_published_ms`
- `queue_wait_ms`
- `artifact_download_ms`
- `checksum_ms`, `image_decode_ms`
- `ocr_ms`, `barcode_ms`, `extraction_ms`, `validation_ms`
- `result_persist_ms`, `worker_total_ms`, `end_to_end_ms`

Ten manual triggers are a functional end-to-end smoke only. Meaningful percentile characterization requires at least 30 samples; 50–100 is preferred when runtime cost permits. Startup/model warmup is reported separately from per-job OCR latency.

Phase 2 does not claim Prometheus/Grafana support unless implemented and verified later.

## 19. Checkpoints

- **2A:** identity, time, contracts, status ownership, prepared/result variants, exact-crop boundary, station/worker split and compatibility façade. No MinIO/RabbitMQ.
- **2B:** atomic spool, immutable job, all three durability paths, checksums, recovery, containment and baseline backpressure.
- **2C:** storage abstraction, MinIO idempotent upload and dispatcher to `ARTIFACTS_READY`.
- **2D:** Rabbit topology, confirmed frozen-job publish, station service and `JOB_PUBLISHED` transition.
- **2E:** resident worker, checksum, existing-result idempotency, durable result and manual ACK.
- **2F:** bounded retry/DLQ, fault matrix, recovery, observability and GX10 runtime acceptance.

The implementation must not proceed from one checkpoint to the next until the preceding acceptance criteria and observed regression results are reported.
