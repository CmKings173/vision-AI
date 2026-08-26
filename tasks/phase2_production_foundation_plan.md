# Phase 2 Production Foundation V1 — Implementation Plan

## 1. Nguyên tắc thực hiện

Plan này triển khai thiết kế tại `tasks/phase2_production_foundation_technical_design.md` theo các checkpoint nhỏ, test-first và có compatibility path. Nó không thay thế roadmap tổng thể trong `tasks/plan.md` hoặc `tasks/todo.md`; trong phạm vi Phase 2, tài liệu này là plan ưu tiên và các hạng mục Redis/TensorRT/YOLO/VLM cũ bị hoãn.

Human pre-2A review gate: **APPROVED**.

Execution status (2026-08-25): **CHECKPOINTS 2A-2F IMPLEMENTED AND TESTED
LOCALLY; GX10/MINIO/RABBITMQ RUNTIME ACCEPTANCE PENDING.** See
`tasks/phase2_implementation_report.md` and `tasks/phase2_code_review.md` for
implemented behavior, verification evidence, and remaining runtime gates.

Các rule bắt buộc:

1. Viết focused failing test trước mỗi behavior change.
2. Không đổi extractor/profile/validator semantics.
3. Không làm station phụ thuộc OCR/ZXing runtime.
4. Không publish job trước durable artifacts.
5. Không ACK trước durable result.
6. Không xóa dữ liệu/spool tự động nếu chưa có policy.
7. Sau mỗi checkpoint chạy focused tests, relevant regression tests, `compileall` và `git diff --check`.
8. Chỉ đánh dấu `RUNTIME VERIFIED` sau khi chạy thật trên GX10 với services thật.

## 2. Target architecture

```text
Process 1: Station
RTSP → acquisition → ring buffer → manual trigger
     → freshness/Top-K → orientation → FixedROI
     → crop/quality → atomic local spool
     → dispatcher → MinIO artifacts → RabbitMQ job

Process 2: Worker
startup → load/warm PP-OCRv6 + ZXing → WORKER READY
RabbitMQ consume → validate/idempotency → MinIO download/checksum
     → OCR → ZXing → extraction → validation
     → MinIO durable result → manual ACK
```

## 3. Proposed package boundaries

Tên module có thể điều chỉnh nhỏ khi implement, nhưng ownership không được nhập nhằng:

```text
src/label_inspection/
  contracts/
    identity.py
    artifacts.py
    job.py
    result.py
    errors.py
  station/
    controller.py
    preparer.py
    spool.py
    dispatcher.py
  storage/
    base.py
    minio_store.py
  messaging/
    topology.py
    publisher.py
    consumer.py
  worker/
    inference_worker.py
```

Existing `camera/`, `preprocessing/`, `detection/`, `quality/`, `ocr/`, `barcode/`, `extraction/` và `validation/` tiếp tục là core modules. Không move file chỉ để khớp sơ đồ thiết kế.

Entrypoint mới dự kiến:

- `scripts/run_station.py`
- `scripts/run_worker.py`

`scripts/manual_rtsp_inspection.py` được giữ như local regression/diagnostic entrypoint.

## 4. Checkpoint 2A — Contracts and boundaries

### Objective

Định nghĩa rõ cross-process contract và tách preparation khỏi inference mà chưa thêm MinIO/RabbitMQ.

### Test-first slices

#### 2A.1 Identity, time và contract validation

Tests trước:

- Accept valid UUIDv4 strings; reject prefix ID, path-like ID và malformed UUID.
- Generate unique `event_id` và `trigger_id`.
- Serialize integer epoch-ms fields; reject float/negative/ambiguous timestamp.
- Contract dùng `received_at_ms` cho thời điểm GX10 nhận frame; optional `source_timestamp_ms` chỉ nhận timestamp source đáng tin cậy và không tự tạo `captured_at_ms` giả.
- Job/result round-trip không mất field.
- Reject unknown contract version.
- Artifact ref bắt buộc bucket/key/sha256/content_type và không nhận bytes/credential.

Implementation dự kiến:

- Thêm `contracts/identity.py`, `contracts/artifacts.py`, `contracts/job.py`, `contracts/result.py`, `contracts/errors.py`.
- Dùng dataclass + explicit `to_dict/from_dict/validate` để tránh dependency mới.
- Contract version khởi đầu rõ ràng, ví dụ `inspection-job.v1` và `inspection-result.v1`.
- Giữ current POC schemas để không break callers.

#### 2A.2 Status semantics

Tests trước:

- Delivery, processing và business status không thể gán nhầm type/value.
- Job trước publish không claim `PUBLISHED`.
- Status ownership được encode/test: station, dispatcher, worker và validator không ghi chồng domain của nhau.
- Quality reject bắt buộc là terminal `COMPLETED + REVIEW`, `inference_executed=false`, không có inference job.
- Preparation technical error bắt buộc là terminal `ERROR + null`, không biến thành business `FAIL`, không có inference job.
- Error result chứa code/stage/retryable/attempt nhưng không chứa secret.

Implementation dự kiến:

- Explicit enums/constants cho ba status domains.
- Envelope result chứa current `InspectionResult` business payload hoặc equivalent serialized payload mà không đổi values.

#### 2A.3 Split preparation and processing

Tests trước:

- Preparer thực hiện orientation → FixedROI → crop → quality và trả exact crop bytes/image.
- Processor nhận prepared crop trực tiếp; không truy cập camera/frame buffer.
- Station-side factory không build/import runtime OCR adapter.
- Compatibility `build_pipeline()` tạo output tương đương fixtures hiện tại.
- Existing sequential OCR → barcode behavior được giữ.

Implementation dự kiến:

- Extract preparation logic khỏi `InspectionPipeline` vào boundary mới.
- Extract candidate completion/inference logic vào processor mới.
- Giữ façade `InspectionPipeline` compose hai phần cho script/test cũ.
- Tách `app.py` thành station/worker factory, vẫn giữ API cũ.

### Acceptance criteria 2A

- Có versioned job/result contracts với UUID và epoch-ms.
- Contract phân biệt quality-pass job, quality-rejected terminal result và preparation-error terminal result.
- Có exact crop ownership rõ ràng.
- Station preparation unit test chạy không cần OCR/ZXing dependencies.
- Existing pipeline contract tests không regression.
- Không có MinIO/RabbitMQ code trong checkpoint này.

### Files có khả năng thay đổi

- New `src/label_inspection/contracts/*`
- New `src/label_inspection/station/preparer.py`
- New worker-side processor module
- `src/label_inspection/pipeline/inspection.py`
- `src/label_inspection/app.py`
- `src/label_inspection/config.py` nếu cần contract/profile versions
- New tests cho contracts/boundaries

## 5. Checkpoint 2B - Atomic local spool and restart recovery

Checkpoint 2B status (2026-08-25): **IMPLEMENTED, TESTED LOCALLY, AND APPROVED
TO PROCEED.** See `tasks/phase2_checkpoint_2b_report.md` for observed evidence.

### Objective

Biến prepared event thành durable local handoff trước bất kỳ network operation nào.

### Test-first slices

#### 2B.1 Safe paths and atomic publish

Tests trước:

- UUID event path luôn nằm trong configured spool root.
- Reject traversal, slash, backslash, drive path và symlink escape.
- Dispatcher không nhìn thấy temp/incomplete event.
- Sau successful rename, event có đủ bốn file bắt buộc.
- `label_crop.png` decode ra đúng pixels/shape đã chuẩn bị.
- Checksum trong job khớp file bytes.
- Frozen `job.json` trong spool byte-for-byte tương ứng payload dispatcher sẽ publish.

Implementation dự kiến:

- Temp directory cùng parent với final event directory.
- Ghi `selected_frame.jpg`, lossless `label_crop.png`, `job.json`, `state.json`.
- Construct complete InspectionJob trước commit; `job.json` immutable sau final rename.
- Flush/fsync phù hợp platform, rồi atomic rename.
- Không dùng `artifacts.py` debug writer làm spool.

#### 2B.2 Durable delivery state

Tests trước:

- State transitions hợp lệ: `LOCAL_ONLY → ARTIFACTS_READY → PUBLISHED`.
- Reject backward/skip transition không được phép.
- `state.json` update atomic.
- Restart scan đọc được final event và bỏ qua temp event.
- Corrupt/missing file bị quarantine/report, không publish.

Implementation dự kiến:

- Spool event API và atomic state update.
- Startup scanner trả pending events theo deterministic order.
- Không auto-delete PUBLISHED events.

#### 2B.3 Terminal attempt durability

Tests trước:

- Quality reject commit `result.json`, không tạo/publish `job.json`.
- Preparation technical error commit `result.json` kể cả không có image artifact.
- Mọi accepted trigger có event identity trước preparation.
- Local commit failure trả structured spool/write error và không claim durable.

Implementation dự kiến:

- Spool API hỗ trợ inference-job và terminal-result variants rõ ràng.
- Dispatcher phân biệt terminal delivery với inference dispatch.

#### 2B.4 Backpressure baseline

Tests trước:

- Reject trigger trước capture nếu spool event/byte threshold vượt ngưỡng.
- Disk-space probe failure fail closed.
- Backpressure tạo structured reason, không giả business FAIL.

Implementation dự kiến:

- Configurable max pending events, bytes và minimum free disk.
- Conservative defaults/documentation; no cleanup.

### Acceptance criteria 2B

- Kill/restart simulation không mất complete local event.
- Partial write không bao giờ được dispatcher publish.
- Mọi artifact path contained trong spool root.
- Không network dependency.

## 6. Checkpoint 2C — MinIO artifact storage and dispatcher

### Objective

Upload durable artifacts từ local spool vào MinIO theo object layout chuẩn và resume được.

### Test-first slices

#### 2C.1 Storage abstraction

Tests trước:

- Put/get/head interface không expose client implementation vào station/worker core.
- Content type, size và SHA-256 được giữ.
- Existing object với đúng checksum là idempotent success.
- Existing object khác checksum là conflict/error, không overwrite âm thầm.

#### 2C.2 MinIO adapter

Tests trước với fake, sau đó integration service:

- Bucket/key layout: bucket `vision-inspections`, prefix station/date/event.
- Upload selected frame, crop và job metadata.
- Credentials không xuất hiện trong logs/errors.
- Network failure giữ state retryable.

#### 2C.3 Dispatcher upload phase

Tests trước:

- Chỉ scan final `LOCAL_ONLY` events.
- Chuyển `ARTIFACTS_READY` chỉ khi tất cả required objects durable.
- Restart tiếp tục từ object còn thiếu.
- Không upload lại object đúng checksum.

### Dependency/config work

- Thêm optional storage dependency đã pin và xác minh GX10 ARM64.
- Bổ sung endpoint, TLS, bucket và secret env names trong `.env.example`.
- Secret fields phải redact trong repr/log.
- Có thể thêm compose riêng cho development nếu được duyệt; không nhúng credential thật.

### Acceptance criteria 2C

- MinIO outage không làm mất local event.
- Recovery sau outage tự resume.
- Worker có thể tải exact crop và verify checksum.
- Chưa publish Rabbit job trong slice upload-only cho đến 2D.

## 7. Checkpoint 2D — RabbitMQ topology and confirmed publish

### Objective

Publish versioned job chỉ sau durable artifacts và có broker confirmation.

### Test-first slices

#### 2D.1 Topology

Tests trước/integration:

- Durable exchange `vision.inspection.x`.
- Routing key `inspection.process`.
- Durable queue `vision.inspection.q`.
- DLQ `vision.inspection.dlq` và dead-letter binding đúng.
- Messages persistent.

#### 2D.2 Confirmed publisher

Tests trước:

- Publisher chỉ nhận event `ARTIFACTS_READY`.
- Publisher confirm success mới chuyển `PUBLISHED`.
- Nack/timeout/connection failure giữ `ARTIFACTS_READY`.
- Restart republish an toàn cùng `event_id`.
- Message body không chứa image bytes hoặc secret.

#### 2D.3 Station persistent loop

Tests trước:

- Startup: spool recovery + acquisition + readiness.
- ENTER tạo một trigger/event UUID, prepare và spool.
- Main loop quay lại `SYSTEM READY` mà không chờ OCR.
- Station shutdown không làm mất final spool event.
- Station process/factory không load PP-OCRv6/ZXing.

Implementation dự kiến:

- `station/controller.py`, `station/dispatcher.py`.
- `messaging/topology.py`, `messaging/publisher.py`.
- `scripts/run_station.py`.

### Acceptance criteria 2D

- Camera/trigger latency được tách khỏi worker inference latency.
- Broker down: event vẫn trong spool và station không claim published.
- Confirmed publish: state durable `PUBLISHED`.
- Backlog threshold bảo vệ disk/station.

## 8. Checkpoint 2E — Resident inference worker

### Objective

Tạo worker process độc lập, load/warmup OCR/ZXing một lần và xử lý job idempotently.

### Test-first slices

#### 2E.1 Worker lifecycle/readiness

Tests trước:

- Consumer không start trước model/barcode warmup thành công.
- `WORKER READY` chỉ emit khi adapters ready và broker connection ready.
- N jobs cùng process chỉ load model một lần.
- Warmup không tính vào per-job OCR latency.

#### 2E.2 Job validation and artifact integrity

Tests trước:

- Reject unsupported schema/invalid UUID/missing ref.
- Download exact `label_crop.png`.
- Checksum mismatch tạo structured non-business error.
- Transient MinIO error là retryable; invalid contract là non-retryable/DLQ candidate.

#### 2E.3 Idempotent processing and durable result

Tests trước:

- Existing valid result với same event ACK ngay, không rerun OCR.
- Existing corrupt/wrong-event result không được coi complete.
- Result envelope preserve raw OCR, barcode, fields, validation và provenance versions.
- Result put failure không ACK.
- Result success rồi ACK failure/redelivery không rerun inference nếu durable result valid.
- Document/test logical durable-result idempotency; không claim exactly-once hoặc strict single inference execution dưới race.

#### 2E.4 Current semantics regression

Tests trước:

- OCR và ZXing tiếp tục sequential.
- DataMatrix independent.
- Current field/profile output không đổi.
- Quality rejection mapping `COMPLETED + REVIEW` không gọi inference.
- Known Nvidia/customer mapping được ghi vào provenance, không tự sửa.

Implementation dự kiến:

- `worker/inference_worker.py`.
- `messaging/consumer.py`.
- `scripts/run_worker.py`.
- Worker factory buộc PP-OCRv6/ZXing production profile được duyệt.

### Acceptance criteria 2E

- Prefetch 1, manual ACK.
- Model resident/reused.
- Durable result trước ACK.
- At-least-once redelivery không tạo conflicting result.
- Result trace được event/trigger/station/camera/profile/locator versions.

## 9. Checkpoint 2F — Reliability, retry, DLQ and runtime verification

### Objective

Chứng minh behavior dưới failure và hoàn tất production-foundation acceptance trên GX10.

### Test-first/fault-injection matrix

| Failure point | Expected behavior |
|---|---|
| Station chết trước atomic rename | Temp/incomplete không publish; startup cleanup/report an toàn |
| Station chết sau rename trước upload | Restart scan và upload |
| MinIO down | Event giữ local state, retry có backoff |
| Station chết sau upload trước publish | Resume publish từ `ARTIFACTS_READY` |
| Broker nack/timeout | Không claim `PUBLISHED` |
| Worker chết trước inference | Rabbit redelivery |
| Worker chết sau inference trước result | Redelivery; có thể infer lại vì chưa durable result |
| Worker chết sau result trước ACK | Redelivery; detect valid result và ACK không infer lại |
| Corrupt crop/checksum | Structured error, bounded retry hoặc DLQ theo classification |
| Poison contract | Không retry vô hạn; vào DLQ |
| Spool threshold vượt ngưỡng | Reject trigger/fail closed, không xóa evidence |

### Retry policy

- Configurable bounded schedule, default `5,30,120` seconds.
- Maximum attempt count explicit.
- Attempt propagated trong headers/result/error.
- Retryable/non-retryable classification testable.
- Sau max attempts, route DLQ và record reason.

### Observability baseline

Mỗi lifecycle event log structured với:

- `event_id`, `trigger_id`, `station_id`, `camera_id`
- component/stage
- delivery/processing/business statuses riêng
- attempt và retryable
- duration/timestamp đúng unit
- safe error code

Timing tối thiểu:

- trigger-to-prepared
- spool write
- artifact upload
- queue wait
- artifact download/checksum
- OCR
- barcode
- extraction/validation
- result persist
- end-to-end

Không claim Prometheus/dashboard nếu chưa implement; structured logs và durable lifecycle fields là baseline Phase 2.

### GX10 runtime acceptance

1. Start MinIO/RabbitMQ test services.
2. Start worker; xác nhận model load/warmup một lần và `WORKER READY`.
3. Start station; xác nhận camera fresh và `SYSTEM READY`.
4. Thực hiện ít nhất 10 manual triggers cùng process như functional E2E smoke.
5. Đối chiếu mỗi event qua spool → MinIO → Rabbit → result.
6. Xác nhận exact crop/checksum và extracted/barcode output.
7. Chạy các fault injections đã duyệt.
8. Functional report không diễn giải p95 từ 10 samples. Performance characterization chỉ report percentile có ý nghĩa khi chạy tối thiểu 30 samples; ưu tiên 50–100 nếu chi phí runtime cho phép.

### Acceptance criteria 2F

- Không mất event trong tested failure matrix.
- Không ACK trước durable result.
- Retry bounded, poison messages tới DLQ.
- Station vẫn capture/prepare khi worker đang bận, trong giới hạn backpressure.
- 10 triggers có trace xuyên suốt bằng UUID.
- Mọi production claim gắn `IMPLEMENTED/TESTED/RUNTIME VERIFIED` rõ ràng.

## 10. Verification command policy

Sau mỗi slice:

```text
focused pytest for changed behavior
relevant existing regression tests
python -m compileall src scripts tests
git diff --check
```

Sau mỗi integration checkpoint:

- Fake/unit tests luôn chạy.
- MinIO/RabbitMQ integration tests chỉ đánh dấu verified khi service thật available.
- Không dùng việc thiếu external service để skip silent; report `NOT VERIFIED` rõ ràng.
- Không chạy full dataset benchmark trong Phase 2 foundation.

## 11. Documentation deliverables

Mỗi checkpoint cập nhật:

- `tasks/phase2_production_foundation_todo.md`
- Contract/state diagrams nếu thay đổi.
- `.env.example` và run instructions khi config mới xuất hiện.
- Provenance/version docs.
- Risk/known blockers nếu evidence mới thay đổi kết luận.

Không ghi đè:

- `tasks/plan.md`
- `tasks/todo.md`
- Phase 1 evaluation docs

## 12. Human review gates

Human review gates:

1. Sau audit/plan này, trước khi code checkpoint 2A — **APPROVED**.
2. Sau 2A, xác nhận contract/status/boundary trước spool.
3. Sau 2B, xác nhận durability/path policy trước network integration.
4. Trước GX10 integration, cung cấp service endpoints/credentials an toàn.
5. Trước bất kỳ thay đổi extractor schema nào, business xác nhận Nvidia/Customer/Our Part Number.

Không có blocker để bắt đầu 2A sau gate số 1.
