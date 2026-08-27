# Phase 2 Production Foundation V1 - Implementation Report

Status: **IMPLEMENTED AND TESTED LOCALLY; GX10 RUNTIME ACCEPTANCE PENDING**

Date: 2026-08-25

Final local verification evidence:

- Ruff: all Phase 2 implementation and test files passed.
- Pytest: `316 passed, 1 skipped, 3 deselected` (`runtime` excluded).
- Fault matrix: three critical regression probes plus the Phase 2 operational
  hardening probes passed.
- `compileall`, source-layout import smoke, station CLI help, and
  `git diff --check` passed.
- The single skip is the Windows symlink-containment test because the host
  lacks symlink-create privilege; it remains a Linux/GX10 runtime gate.

This report covers checkpoints 2C through 2F. Checkpoints 2A and 2B retain
their dedicated reports. `tasks/phase2_production_foundation_technical_design.md`
remains the architecture authority.

The Phase 2 delivery infrastructure is now defined in
`infra/phase2/docker-compose.yml`: MinIO, idempotent bucket/app-user bootstrap,
and RabbitMQ run with persistent volumes while station and worker remain native
GX10 processes. Compose parsing is locally verified; container startup and real
service behavior remain runtime gates.

## Delivered architecture

```text
station-service
  RTSP -> bounded FrameBuffer -> manual trigger -> Top-K/orientation/FixedROI
  -> quality -> exact PNG + immutable job -> atomic LocalSpool
  -> background create-only MinIO upload -> confirmed RabbitMQ publish

inference-worker
  MinIO check -> load/warm PP-OCRv6 + ZXing once -> WORKER_READY
  -> manual-ACK Rabbit consume -> validate job/result idempotency
  -> exact crop download + SHA-256 -> sequential OCR/ZXing/extraction/validation
  -> create-only durable result -> ACK
  -> retry 5/30/120 seconds or final DLQ on technical failure
```

Station does not import/build/call OCR or ZXing. Worker does not access RTSP,
the ring buffer, FixedROI, or crop reconstruction.

## 2C - Storage and dispatcher

- Added typed `ArtifactStore` contract and deterministic in-memory fault fake.
- Added station/date/event object keys with strict station and UUID validation.
- Pinned `minio==7.2.20` in the `phase2` optional dependency.
- Added a create-only MinIO adapter using `If-None-Match: *`, exact SHA-256,
  content type and byte-size metadata, plus read-back verification.
- Same key/same bytes is idempotent. Same key/different bytes is a hard
  immutable conflict and is never overwritten.
- Isolated minio-py's private single-part `_put_object` compatibility surface;
  adapter construction fails if the pinned primitive is absent. This must be
  revalidated before changing the pinned SDK.
- Dispatcher uploads selected frame, exact crop and frozen job before moving
  inference records to `ARTIFACTS_READY`.
- Station terminal records upload available evidence/result and end at
  `TERMINAL_RESULT_DURABLE` without creating an inference message.
- A failed record does not starve later committed records. Partial uploads
  stay `LOCAL_ONLY` and converge through idempotent retry.
- Station startup does not probe MinIO; camera/local spool remain usable during
  an object-store outage until configured backpressure is reached.
- Station startup validates local configuration/spool capacity, starts camera
  acquisition, and becomes capture-ready before any MinIO connection attempt.
  The delivery pump connects lazily; its health is `NOT_CHECKED` before the
  first delivery attempt and `DEGRADED` when MinIO/Rabbit delivery is down.

## 2D - RabbitMQ and station service

- Pinned `pika==1.4.4` in the `phase2` optional dependency.
- Durable direct exchange, main queue, retry queues and final DLQ.
- Main routing: `vision.inspection.x` / `inspection.process` /
  `vision.inspection.q`; final DLQ: `vision.inspection.dlq`.
- Persistent JSON messages, mandatory routing and publisher confirms.
- `message_id` and `correlation_id` are the UUID `event_id`.
- Publisher sends the exact committed `job.json` bytes and advances to
  `JOB_PUBLISHED` only after confirmation.
- Persistent station service runs independent camera and delivery threads.
  Trigger completion means atomic local durability, not network delivery.
- `scripts/run_station.py` is the station entrypoint and records exact
  extraction profile/version/mapping provenance plus the actual FixedROI or
  YOLO locator version/config/model SHA-256.

## 2E - Resident worker

- `scripts/run_worker.py` initializes MinIO, loads/warms PP-OCRv6 and ZXing,
  then connects RabbitMQ and emits `WORKER_READY`.
- Phase 2 fails fast if configured with legacy `ppocr`; resident PP-OCRv6 is
  required by this vertical slice.
- Prefetch is 1 and consumer auto-ACK is disabled.
- Worker validates `inspection-job.v1`, downloads only the referenced exact
  crop, verifies bytes and decodes the PNG directly.
- It never reads the selected-frame object and never reruns orientation,
  FixedROI or crop.
- Existing valid `inspection-result.v1` with matching event/trigger/station/
  camera identity skips inference and is ACK-safe.
- OCR runs inline on its caller/warmup thread while ZXing runs on one background
  worker. They overlap, then extraction/validation waits for both results.
- Durable result contains raw OCR, extracted fields, barcode evidence,
  validation, quality, semantic provenance and stage timings.
- ACK occurs only after result create/read/checksum verification.

## 2F - Retry, DLQ and observability

- Retry delays are configured through `VISION_RETRY_DELAYS_MS`; default is
  `5000,30000,120000`, bounded to 1-10 increasing positive values.
- Retry queues use per-queue TTL and dead-letter back to the processing route.
  Retry handoff is confirmed before ACK of the original delivery.
- Non-retryable integrity/contract failures and exhausted retryable failures
  are confirmed to the final DLQ before ACK.
- Failed retry/DLQ confirmation leaves the original message unacked; worker
  closes the connection so RabbitMQ can redeliver it.
- Structured one-line JSON lifecycle events carry event/component/stage/status,
  safe error code and available timing fields. Sensitive field names are
  rejected.
- Local fault tests cover atomic rename failure, checksum corruption, partial
  upload, MinIO outage, Rabbit NACK, failed retry confirm, final DLQ, duplicate
  result, persist failure-before-ACK and poisoned-record isolation.

## R9 operational hardening

- Runtime validates the pre-provisioned artifact bucket at startup; station and
  worker processing never create buckets per artifact.
- `image_download_ms`, `checksum_ms` and `image_decode_ms` are measured as
  exclusive stages. Model load/warmup is startup-only and excluded from OCR
  latency.
- Broker consume loss emits `BROKER_CONNECTION_LOST` and exits so the supplied
  systemd unit can restart the worker. See
  `tasks/phase2_operations_runbook.md`.
- MinIO 7.2.20 and Pika 1.4.4 are exact controlled requirements in
  `pyproject.toml`; `uv.lock` records the resolved transitive dependency graph.

## Run on GX10

Install Phase 2 transport and the existing PP-OCRv6/ZXing runtime:

```bash
cd ~/Projects/vision-AI
source .venv/bin/activate
python3 -m pip install -e '.[phase2,ocr-transformers,barcode]'
```

Configure real values outside Git:

```bash
export VISION_MINIO_ENDPOINT='127.0.0.1:9000'
export VISION_MINIO_ACCESS_KEY='...'
export VISION_MINIO_SECRET_KEY='...'
export VISION_MINIO_SECURE=false
export VISION_ARTIFACT_BUCKET='vision-inspections'
export VISION_RABBITMQ_URL='amqp://vision:...@127.0.0.1:5672/%2F'
export VISION_RETRY_DELAYS_MS='5000,30000,120000'

export VISION_OCR_ENGINE='ppocr_v6'
export VISION_OCR_BACKEND='transformers'
export VISION_OCR_VERSION='PP-OCRv6'
export VISION_OCR_DEVICE='gpu:0'
export VISION_BARCODE_ENGINE='zxing'
export VISION_EXTRACTION_PROFILE='dgx_spark_label'
export VISION_REQUIRED_FIELDS='customer_part_number,so_number,our_part_number,quantity,net_weight,gross_weight,carton_number'

export VISION_RTSP_URL='http://10.10.12.13:8080/video'
export VISION_LABEL_ROI='CALIBRATED_X1,CALIBRATED_Y1,CALIBRATED_X2,CALIBRATED_Y2'
export VISION_CAMERA_ROTATE_DEG=0
export VISION_SPOOL_ROOT="$HOME/Projects/vision-AI/spool"
```

Terminal 1:

```bash
python3 scripts/run_worker.py
```

Wait for `WORKER_READY`. Terminal 2:

```bash
python3 scripts/run_station.py \
  --source "$VISION_RTSP_URL" \
  --roi "$VISION_LABEL_ROI" \
  --rotate-deg "$VISION_CAMERA_ROTATE_DEG" \
  --triggers 10
```

Expected path per trigger:

```text
LOCAL_COMMIT -> ARTIFACTS_READY -> JOB_PUBLISHED
Rabbit delivery -> durable result/result.json -> ACK
```

For runtime fault acceptance, stop MinIO before a trigger and verify
`LOCAL_COMMIT` still succeeds; restart MinIO and verify recovery. Then stop
RabbitMQ after artifact upload and verify the event remains `ARTIFACTS_READY`;
restart it and verify `JOB_PUBLISHED`. Kill the worker before durable result and
verify redelivery converges on one logical result. Do not delete spool evidence.

Ten triggers are functional acceptance only. Use at least 30 completed jobs
for percentile characterization; 50-100 is preferred. Lifecycle logs contain
queue/download/checksum/OCR/barcode/extraction/validation/persist/total timings.

## Verification status

| Claim | Status |
|---|---|
| Contracts, exact-crop boundary, spool and status ownership | TESTED LOCALLY |
| MinIO adapter against deterministic fake | TESTED LOCALLY |
| Rabbit publish/ACK/retry against deterministic fake | TESTED LOCALLY |
| Local station-to-worker contract integration | TESTED LOCALLY |
| Real MinIO conditional PUT | NOT RUNTIME VERIFIED |
| Real Rabbit confirms, TTL/DLX and reconnect | NOT RUNTIME VERIFIED |
| Linux symlink containment | NOT RUNTIME VERIFIED |
| GX10 PP-OCRv6/ZXing in two persistent processes | NOT RUNTIME VERIFIED |
| 10-trigger GX10 acceptance | NOT RUN |
| 30+ job production latency percentiles | NOT RUN |

## Invariants and blockers

- Never publish before required objects are durable.
- Never ACK before a durable result or confirmed retry/DLQ handoff.
- Never reconstruct the crop or rebuild frozen `job.json`.
- Never automatically delete spool evidence without approved policy.
- Delivery is at-least-once with logical result idempotency, not exactly-once
  inference execution.
- `Nvidia P/N <-> Customer Part Number <-> Our Part Number` remains
  `KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION`. Current mapping is
  preserved; `customer_part_number` is not claimed production-verified.
