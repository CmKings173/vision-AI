# Phase 2 Production Foundation V1 — Repository Audit

This is a historical pre-implementation snapshot. Its original `PUBLISHED`
state references describe the design at that time and are not the current
delivery state; current states are documented in the technical design.

## 1. Mục đích và phạm vi

Tài liệu này ghi lại hiện trạng repository trước khi triển khai Phase 2 Production Foundation V1. Nó là cầu nối giữa code POC đang chạy và thiết kế kỹ thuật tại `tasks/phase2_production_foundation_technical_design.md`.

Phạm vi audit:

- Luồng RTSP → ring buffer → manual trigger → Top-K → orientation → FixedROI → quality → PP-OCRv6/ZXing → extraction → validation → JSON.
- Entrypoint, module boundary, schema, config, dependency và test hiện có.
- Mapping từng thành phần Phase 2 thành `REUSE`, `REFACTOR` hoặc `NEW`.
- Gap, rủi ro, invariant và blocker trước checkpoint 2A.

Ngoài phạm vi:

- Không triển khai code Phase 2 trong audit này.
- Không thay đổi semantics PP-OCRv6, ZXing, extractor hoặc validator.
- Không thêm YOLO, VLM, TensorRT, PostgreSQL, Redis, Kafka hoặc Kubernetes.
- Không chạy full dataset benchmark.
- Không sửa/xóa dữ liệu trong `datasets/`, `dynamsoft/` hoặc `phase1_input/`.

## 2. Trạng thái audit

Các nhãn trong tài liệu:

- `IMPLEMENTED`: có implementation trong repository.
- `TESTED`: có test tự động tương ứng trong repository.
- `RUNTIME VERIFIED`: đã có bằng chứng chạy thật trên GX10 từ POC trước đó.
- `NOT VERIFIED`: chưa chạy hoặc chưa đủ bằng chứng trong môi trường audit hiện tại.

Snapshot audit:

| Thuộc tính | Giá trị |
|---|---|
| Ngày audit | 2026-08-25 |
| Branch | `main` |
| Commit quan sát | `7543efb` — `Fix split customer part number extraction` |
| Remote tracking | `origin/main` cùng commit tại thời điểm audit |
| Worktree ngoài scope | `datasets/`, `dynamsoft/`, `phase1_input/` đang untracked và không bị thay đổi |
| Test trên máy audit | `NOT VERIFIED`: Python hiện tại không có module `pytest` |
| Compile/runtime Phase 2 | `NOT IMPLEMENTED` |

Lưu ý: các kết quả test hoặc benchmark được ghi trong tài liệu cũ là bằng chứng lịch sử, không được coi là test vừa chạy trong audit này.

## 3. Kiến trúc hiện tại

### 3.1 Luồng runtime POC đang active

```text
IP camera RTSP/HTTP
        │
        ▼
RTSPCamera (OpenCV/FFmpeg, reconnect/backoff)
        │ background read thread
        ▼
CameraAcquisition
        │ FramePacket
        ▼
FrameRingBuffer (bounded deque)
        │ snapshot on ENTER
        ▼
Freshness filter + Top-K FrameSelector
        │
        ▼
Orientation normalization (script level)
        │
        ▼
InspectionPipeline
  FixedROI → crop/rectify → quality/ranking
        │
        ├─ quality rejected → REVIEW, inference skipped
        │
        └─ accepted crop
             ├─ PP-OCRv6 DET+REC
             ├─ ZXing barcode       (sequential, not parallel)
             ├─ FieldExtractor
             └─ LabelValidator
        │
        ▼
InspectionResult + debug artifacts + terminal JSON
```

Acquisition đã chạy nền và ring buffer đã bounded. Phần còn synchronous là manual-trigger processing: từ snapshot cho đến OCR, barcode, extraction, validation và ghi artifact đều nằm trong process/flow của station. Vì vậy mô tả chính xác là **asynchronous acquisition nhưng synchronous inspection and delivery**.

### 3.2 Entrypoint active

Entrypoint acceptance hiện tại là `scripts/manual_rtsp_inspection.py`.

Nó đang chịu đồng thời các trách nhiệm:

1. Build toàn bộ pipeline, bao gồm OCR và barcode.
2. Load/warmup PP-OCRv6 và ZXing.
3. Kết nối camera và khởi động acquisition thread.
4. Chờ camera fresh rồi emit `SYSTEM READY`.
5. Nhận manual trigger.
6. Snapshot buffer, rotate frame và gọi pipeline synchronous.
7. Tái tạo label crop từ bbox để ghi debug artifact.
8. In result/benchmark ra terminal.
9. Dừng acquisition và đóng native video backend.

Đây là composition root phù hợp POC nhưng là coupling chính cần tách trong Phase 2.

Các script khác là diagnostic/evaluation entrypoint, không phải station production:

| Script | Vai trò hiện tại | Phase 2 |
|---|---|---|
| `scripts/manual_rtsp_inspection.py` | POC acceptance chính | Giữ làm regression/diagnostic; không biến trực tiếp thành distributed station |
| `scripts/camera_smoke.py` | Kiểm tra RTSP acquisition | Giữ |
| `scripts/inspect_rtsp.py` | One-shot RTSP inspection | Giữ diagnostic |
| `scripts/run_real_rtsp_integration.py` | Integration POC cũ | Giữ diagnostic |
| `scripts/inspect_image.py` | One-image inspection | Giữ diagnostic |
| `scripts/run_real_image_integration.py` | Real-image benchmark | Giữ diagnostic |
| `scripts/evaluate_dataset.py` | Phase 1 evaluator | Không ghép vào Phase 2 runtime |
| `scripts/build_tensorrt_engine.py` | Deferred TensorRT path | Ngoài scope |

### 3.3 Module map hiện tại

| Khu vực | Module chính | Hiện trạng |
|---|---|---|
| Camera contract | `src/label_inspection/camera/base.py` | Protocol transport-neutral, reusable |
| Acquisition | `camera/acquisition.py` | Background daemon thread, bounded stop/wait |
| Ring buffer | `camera/frame_buffer.py` | Thread-safe bounded deque, freshness filter, condition wait |
| RTSP transport | `camera/rtsp.py` | OpenCV/FFmpeg, timeout, reconnect/backoff, health, careful close |
| Source security | `camera/security.py` | URL precedence và mask password |
| Top-K selection | `camera/selector.py` | Freshness + preview quality ranking, giữ full frame |
| Orientation | `preprocessing/orientation.py` | Quarter-turn normalization; đang được gọi ở script/evaluator |
| ROI/detection | `detection/fixed_roi.py` | Fixed normalized ROI có clamp/validation |
| Crop/rectify | `preprocessing/crop.py`, `rectify.py` | Active |
| Quality | `quality/metrics.py` | Active, có quality gate |
| Candidate ranking | `pipeline/ranking.py` | Active |
| Orchestrator | `pipeline/inspection.py` | Monolithic sync prep + inference + business logic |
| OCR | `ocr/ppocr_v6.py` | Resident lazy model, explicit warmup, reuse model |
| Barcode | `barcode/zxing.py` | Reusable, multi-code, variant decode |
| Extraction | `extraction/fields.py`, `profiles.py` | Deterministic profile hiện tại |
| Validation | `validation/rules.py` | Required fields/barcode/confidence |
| Schemas | `schemas.py` | Local POC schemas, chưa có distributed contracts |
| Timing | `timing.py` | Local stage timings dùng monotonic clock |
| Composition | `app.py` | Factory hiện tại luôn build cả preparation và inference |
| Artifacts | `artifacts.py` | Debug evidence, không phải durable spool |
| Evaluation | `evaluation/*` | Phase 1 only; không thuộc runtime Phase 2 |

## 4. Data và control flow chi tiết

### 4.1 Camera và ring buffer

- `RTSPCamera.read()` trả `FramePacket` có frame, wall-clock seconds, monotonic seconds và source.
- `CameraAcquisition` đọc liên tục trong daemon thread, tăng `captured_count` và append packet vào `FrameRingBuffer`.
- `FrameRingBuffer` dùng `deque(maxlen=...)`; frame cũ tự bị loại khi đầy.
- Trigger không yêu cầu camera đọc lại; nó snapshot các frame mới nhất đã có trong buffer.
- Freshness ưu tiên monotonic timestamp, tránh wall-clock adjustment.
- `RTSPCamera.health()` cung cấp connected/stale/frame count/reconnect/last error.

Đánh giá: `IMPLEMENTED`, `TESTED`; camera/ring-buffer đã `RUNTIME VERIFIED` trên GX10 theo POC.

### 4.2 Trigger và frame selection

- Manual ENTER hoặc scheduled trigger nằm trong script.
- Mỗi trigger snapshot buffer, loại stale packet và chọn Top-K bằng brightness/sharpness preview.
- Frame full resolution chỉ được dùng sau ranking.
- Event ID hiện tại có dạng `INS-{12 hex}`, không phải UUID contract Phase 2.
- Chưa có `trigger_id` riêng.

Đánh giá: POC `IMPLEMENTED`, `TESTED`, `RUNTIME VERIFIED`; identity contract Phase 2 `NOT IMPLEMENTED`.

### 4.3 Preparation và inference

`InspectionPipeline.inspect_packets()` hiện thực hiện cả hai nửa:

```text
Preparation-owned                         Worker-owned in Phase 2
---------------------------------------   ---------------------------------
freshness / Top-K                         PP-OCRv6
FixedROI                                  ZXing
crop / rectify                            field extraction
quality / candidate ranking               validation
choose exact label crop                   result construction
```

Hiện tại boundary này chưa tồn tại trong code. OCR và barcode chạy tuần tự trên best candidate. Quality reject sẽ trả `REVIEW` và không chạy OCR/barcode.

Rủi ro quan trọng: `PreparedCandidate.image` mới là exact image inference đã dùng, nhưng manual script đang tái tạo crop từ bbox sau khi pipeline trả result. Hai ảnh thường giống nhau với FixedROI hiện tại, nhưng không có invariant bảo đảm khi rectify/orientation thay đổi. Phase 2 phải persist chính xác prepared crop, không reconstruct.

### 4.4 Business semantics

- Extractor và validator hiện tại là deterministic production semantics của POC.
- `customer_part_number`, `so_number`, `our_part_number`, `quantity`, `net_weight`, `gross_weight`, `carton_number` đang là string-valued extracted fields.
- DataMatrix được giữ độc lập từ ZXing.
- Mapping `Nvidia P/N → customer_part_number` vẫn tồn tại theo quyết định trước và là `KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION`.
- Phase 2 không được tự sửa mapping này, không thêm `nvidia_part_number`, không hợp nhất hoặc tách field theo suy đoán.

Thiết kế có ví dụ result với numeric normalized fields. Ví dụ đó không được dùng để âm thầm thay đổi type/semantics đang chạy. Phase 2 nên bọc/preserve payload business hiện tại trong versioned result envelope; thay đổi business schema phải là change riêng sau confirmation.

## 5. Mapping thiết kế Phase 2 vào codebase

### 5.1 Thành phần có thể reuse gần như nguyên vẹn

| Design component | Existing code | Quyết định | Lý do |
|---|---|---|---|
| RTSP camera | `camera/base.py`, `camera/rtsp.py` | `REUSE` | Có timeout, reconnect, health, close coordination |
| Background acquisition | `camera/acquisition.py` | `REUSE` | Đã chạy nền, không block controller |
| Bounded ring buffer | `camera/frame_buffer.py` | `REUSE` | Thread-safe, maxlen, wait, stale filtering |
| Freshness + Top-K | `camera/selector.py` | `REUSE` | Đúng flow acceptance hiện tại |
| Orientation primitive | `preprocessing/orientation.py` | `REUSE` | Cần chuyển ownership vào station preparer |
| FixedROI | `detection/fixed_roi.py` | `REUSE` | Không thêm detector mới |
| Crop/rectify/quality/ranking | Existing preprocessing/quality/pipeline modules | `REUSE` | Giữ CV semantics |
| PP-OCRv6 resident adapter | `ocr/ppocr_v6.py` | `REUSE` | Có load once/warmup/reuse |
| ZXing | `barcode/zxing.py` | `REUSE` | Có prepare, decode và multi-code |
| Extractor/profile | `extraction/*` | `REUSE` | Không đổi business mapping |
| Validator | `validation/*` | `REUSE` | Không đổi PASS/REVIEW/FAIL rules |
| Stage timing helper | `timing.py` | `REUSE + EXTEND` | Giữ monotonic duration, thêm lifecycle timings |

### 5.2 Thành phần cần refactor có compatibility path

| Existing code | Vấn đề | Refactor tối thiểu |
|---|---|---|
| `pipeline/inspection.py` | Gộp preparation, inference và business validation | Tách preparer/processor; giữ `InspectionPipeline` façade cho script/test cũ |
| `app.py` | Factory station luôn load OCR/ZXing | Tách `build_preparer()` và `build_processor()`; giữ `build_pipeline()` compatibility |
| `schemas.py` | Local result không có distributed lifecycle | Giữ schema POC; thêm contracts versioned riêng, tránh breaking import |
| `FramePacket` | Seconds và internal fields chưa khớp epoch-ms contract | Adapter/serialization boundary sang integer epoch-ms, giữ monotonic chỉ nội bộ |
| `scripts/manual_rtsp_inspection.py` | Một file sở hữu cả station + worker | Giữ diagnostic; entrypoint mới dùng module Phase 2 |
| `artifacts.py` | Debug-only, non-atomic | Không nâng cấp ngầm thành spool; chỉ reuse serialization/evidence conventions phù hợp |
| Config wiring | Chưa có distributed settings | Thêm config typed/validated, không log secret |

### 5.3 Thành phần mới bắt buộc

| Thành phần mới | Trách nhiệm |
|---|---|
| Versioned job/result contracts | Validate schema, UUID, epoch-ms, artifact refs, processing/delivery/business status |
| Identity/time helpers | `event_id`, `trigger_id`, created/captured/prepared/published timestamps |
| Station controller | READY, trigger, freshness, preparation, spool handoff; tuyệt đối không gọi OCR |
| Local spool | Atomic directory publish, checksums, state transitions, restart scan/resume |
| Artifact store abstraction | Upload/download/existence/result durability boundary |
| MinIO adapter | Bucket/key layout, SHA-256, content type, idempotent upload |
| Outbox dispatcher | Scan spool, upload artifacts, publish confirmed job, update delivery state |
| RabbitMQ topology/publisher | Durable exchange/queue/DLQ, persistent messages, confirms |
| RabbitMQ worker consumer | Prefetch 1, manual ACK/NACK, bounded retry/DLQ |
| Inference worker | Load/warmup once, validate/download/checksum/process/persist result/ACK |
| Station/worker entrypoints | Hai process độc lập với readiness và graceful shutdown |
| Phase 2 tests | Contract, durability, recovery, idempotency, redelivery, DLQ, checksum, status separation |

### 5.4 Impact list dự kiến

| Khu vực | Mức tác động | Dự kiến |
|---|---|---|
| `camera/*` | Thấp | Reuse; chỉ sửa nếu contract adapter thực sự cần, không rewrite acquisition |
| `preprocessing/*`, `detection/*`, `quality/*` | Thấp | Reuse; thêm regression around exact prepared crop |
| `pipeline/inspection.py` | Cao nhưng có compatibility | Tách prep/process, giữ façade và output POC |
| `app.py` | Cao nhưng có compatibility | Tách composition root station/worker, giữ `build_pipeline()` |
| `schemas.py` | Thấp | Không ép distributed contract vào local schema; ưu tiên package mới |
| `contracts/*` | Mới | Cross-process schema, identity, states, refs, errors |
| `station/*` | Mới | Controller, preparer, spool, dispatcher |
| `storage/*` | Mới | Storage protocol và MinIO adapter |
| `messaging/*` | Mới | Rabbit topology, publisher và consumer |
| `worker/*` | Mới | Resident inference worker và idempotent result flow |
| `scripts/run_station.py`, `scripts/run_worker.py` | Mới | Production-foundation entrypoints |
| Existing diagnostic scripts | Thấp | Giữ hoạt động qua compatibility path |
| `config.py`, `.env.example`, `pyproject.toml` | Trung bình | Typed distributed settings và optional pinned dependencies |
| `tests/*` | Cao | Thêm contract, fault, integration và regression coverage |
| Phase 1 evaluator/datasets | Không tác động | Không import vào Phase 2 runtime, không sửa dữ liệu |

## 6. Contract và state gaps

### 6.1 Identity và timestamps

Hiện tại:

- `event_id`: short prefixed hex.
- `trigger_id`: không có.
- timestamps: mixed float seconds; naming không thể hiện unit.

Phase 2 yêu cầu:

- `event_id` và `trigger_id` là valid UUID string. Đề xuất dùng stdlib UUIDv4 để không thêm dependency.
- Contract fields dùng integer epoch milliseconds và suffix `_ms`.
- Với RTSP hiện tại, `received_at_ms` là thời điểm GX10 nhận/ghi frame; không được gọi sai thành physical camera capture time.
- `source_timestamp_ms` chỉ xuất hiện khi source cung cấp timestamp đáng tin cậy. `captured_at_ms` chỉ dùng nếu capture semantics thật đã được chứng minh và document.
- Monotonic clock chỉ dùng nội bộ để tính timeout/duration; không serialize như business timestamp.
- Mọi log/job/result/artifact path phải carry cùng `event_id`.

### 6.2 Ba state machine độc lập

Không được dùng một field `status` cho ba loại trạng thái.

Delivery:

```text
LOCAL_ONLY → ARTIFACTS_READY → PUBLISHED
```

Processing:

```text
CREATED → CAPTURED → PREPARED → QUEUED → PROCESSING → COMPLETED
                                                    └→ ERROR
```

Business:

```text
null | PASS | REVIEW | FAIL
```

Quality reject bắt buộc trở thành terminal station result với `processing_status=COMPLETED`, `business_status=REVIEW`, `inference_executed=false`; không publish inference job và không biến nó thành lỗi delivery/worker.

Preparation technical error bắt buộc trở thành terminal result với `processing_status=ERROR`, `business_status=null` và structured error. Nó không trở thành business `FAIL` và không publish inference job.

### 6.3 Artifact reference

Contract phải chứa reference, không chứa image bytes hoặc credential:

```json
{
  "bucket": "vision-inspections",
  "key": "station/date/event/label_crop.png",
  "sha256": "...",
  "content_type": "image/png"
}
```

Worker phải verify checksum trước inference. `label_crop.png` phải là exact image do station preparer tạo ra.

### 6.4 Error contract

Current pipeline chủ yếu map exception thành error code coarse. Phase 2 cần tối thiểu:

- `code`
- `message` đã redact secret
- `stage`
- `retryable`
- `attempt`
- optional safe details

Không serialize traceback chứa credentials/URL raw vào message/result.

## 7. Durability và reliability gaps

### 7.1 Local artifact writer không phải spool

`artifacts.py` hiện tại:

- Tạo event directory trực tiếp.
- Ghi từng file không có temp-dir publish.
- Không fsync/atomic rename.
- Không có `state.json` durable state machine.
- Không checksum.
- Không startup scan/resume.
- Không enforce output containment/event allowlist theo contract.
- Crop dùng JPEG debug, trong khi Phase 2 yêu cầu inference crop lossless PNG.

Do đó phải tạo spool module riêng. Atomic invariant:

1. Temp directory và final directory cùng parent/filesystem.
2. Ghi `selected_frame.jpg`, `label_crop.png`, `job.json`, `state.json` vào temp.
3. Flush/fsync file khi platform hỗ trợ.
4. Atomic rename temp → final.
5. Chỉ final directory mới được dispatcher nhìn thấy.
6. Mọi resolved path phải nằm dưới spool root.

Mỗi manual trigger được hệ thống chấp nhận phải có `event_id` trước preparation và phải đi vào một trong ba local durable paths: inference job, quality-rejected terminal result hoặc preparation-error terminal result. Nếu local commit thất bại, hệ thống phải report `ARTIFACT_WRITE_ERROR`/`SPOOL_COMMIT_ERROR` và không claim durability.

Với quality PASS, complete `job.json` được tạo một lần trước atomic commit. Sau commit nó immutable; dispatcher chỉ upload và publish exact frozen payload, không rebuild/mutate job sau restart hoặc config/code change.

### 7.2 Idempotency và ACK

Hiện chưa có message broker hoặc durable result. Worker Phase 2 phải:

1. Validate job.
2. Kiểm tra durable result hiện có.
3. Nếu result tồn tại, validate schema và `event_id`; chỉ khi hợp lệ mới ACK duplicate.
4. Download artifact và verify checksum.
5. Process một lần theo current semantics.
6. Persist result durable vào MinIO.
7. Xác nhận persistence thành công.
8. Sau cùng mới ACK RabbitMQ.

`ACK before durable result` là invariant cấm phá.

Guarantee chính xác của V1 là **at-least-once delivery + logical idempotency of the durable result**. V1 không guarantee exactly-once hoặc strict single execution of inference dưới concurrent workers/crash races; inference có thể chạy lại trước khi durable result được nhìn thấy.

### 7.3 Retry, DLQ và backpressure

Chưa có implementation cho:

- Publisher confirm.
- Manual ACK.
- Bounded retry 5/30/120 giây.
- Retry count/header.
- DLQ sau giới hạn.
- Prefetch 1.
- Disk/backlog threshold.
- Stop accepting trigger khi spool không còn an toàn.

Đây là scope chính của checkpoint 2D–2F.

## 8. Config và dependencies

### 8.1 Hiện tại

`pyproject.toml` có các nhóm cho base CV, detector, OCR, barcode, Redis transport và dev test. Không có MinIO/RabbitMQ client.

`.env.example` hiện có camera/CV/OCR/barcode/quality settings nhưng thiếu:

- `station_id`
- spool root/retention/threshold
- MinIO endpoint/bucket/access/secret/TLS
- RabbitMQ URL/exchange/routing-key/queue/DLQ/prefetch/retry
- worker/profile/version settings

Group `transport=redis` là roadmap cũ, không phải transport được duyệt cho Phase 2. Không được kéo Redis vào implementation này.

### 8.2 Dependency decision đề xuất

Tại checkpoint tương ứng, thêm optional extras tách biệt cho storage và messaging. Client cụ thể phải là thư viện sync, ổn định, hỗ trợ ARM64/Python trên GX10 và có publisher confirms/manual ACK. Lựa chọn cuối cùng cần được pin/test ở 2C/2D; không cần quyết định để bắt đầu 2A.

Không thêm framework web hoặc schema framework mới chỉ để phục vụ contract. Phương án ít thay đổi nhất là dataclass + explicit validation/serialization, cùng phong cách code hiện tại.

## 9. Test coverage audit

### 9.1 Coverage đã có

| Hành vi | Test hiện có |
|---|---|
| Ring buffer bounded/fresh/wait | `test_frame_buffer.py` |
| Acquisition độc lập và blocking-read shutdown | `test_camera_hardening.py` |
| RTSP reconnect/timeout/health/close | `test_rtsp.py` |
| Top-K/ranking/single OCR | `test_selector.py`, `test_candidate_ranking.py` |
| Orientation | `test_orientation.py` |
| Local sync pipeline contracts/errors | `test_pipeline_contract.py` |
| PP-OCR load once/warmup | `test_ppocr_v6.py` |
| ZXing/validation/extraction | `test_barcode.py`, `test_validation.py`, `test_dgx_spark_fields.py` |
| Debug artifacts | `test_artifacts.py` |
| Config/schema/smoke | `test_config_wiring.py`, `test_schema.py`, `test_smoke.py` |

### 9.2 Coverage còn thiếu

- UUID/event/trigger propagation.
- Epoch-ms serialization.
- Versioned contract accept/reject matrix.
- Distinct processing/delivery/business status.
- Atomic spool visibility, containment và interrupted-write recovery.
- Startup resume từ từng delivery state.
- Checksum tamper/mismatch.
- MinIO idempotent upload/download/result validation.
- Publisher confirm failure.
- Rabbit durable topology, persistent message, prefetch 1.
- Worker manual ACK ordering.
- Crash sau inference nhưng trước result, và sau result nhưng trước ACK.
- Duplicate/redelivered job idempotency.
- Bounded retry và DLQ.
- Backpressure/disk-full behavior.
- Secret redaction.
- Station process không import/build/call OCR.
- Cross-process end-to-end trên GX10.

## 10. Coupling quan trọng và invariant không được phá

1. Station chỉ chịu camera, selection, orientation, FixedROI, crop, quality, spool và dispatch; station không load/call OCR hoặc ZXing.
2. Worker load/warmup PP-OCRv6 và ZXing một lần trước `WORKER READY`, reuse qua nhiều job.
3. OCR và barcode giữ sequential behavior; không thêm parallel optimization trong Phase 2.
4. Exact `label_crop.png` được persist rồi worker dùng chính artifact đó.
5. Raw OCR evidence vẫn giữ Nvidia P/N riêng trong OCR lines; extractor mapping hiện tại không bị tự sửa.
6. Business PASS/REVIEW/FAIL không được dùng thay cho delivery/processing state.
7. Job chỉ publish sau khi local spool complete và MinIO artifacts đã durable.
8. Publisher phải dùng confirm; worker chỉ ACK sau durable result.
9. Retry có bound; poison job phải đi DLQ, không loop vô hạn.
10. At-least-once delivery đòi hỏi idempotency theo `event_id`.
11. Không log camera password, MinIO secret, Rabbit credentials hoặc URL chưa mask.
12. Không xóa spool/artifact theo suy đoán khi chưa có retention policy; disk pressure phải fail closed.
13. Existing diagnostic scripts và Phase 1 evaluator phải tiếp tục chạy qua compatibility path.
14. Không thay đổi current extractor/profile/validator semantics trong Phase 2 foundation.
15. `job.json` đã local-commit là immutable và Rabbit payload phải tương ứng exact frozen payload đó.
16. Mỗi accepted trigger phải có durable local attempt record nếu local disk operational, kể cả quality reject hoặc preparation error.
17. Status owner duy nhất: station sở hữu pre-processing/terminal station states; spool/dispatcher sở hữu delivery; worker sở hữu worker processing; validator sở hữu business status.

## 11. Gaps theo mức ưu tiên

### Critical cho production foundation

- Chưa có durable local spool hoặc crash recovery.
- Chưa có MinIO/RabbitMQ integration.
- Chưa có versioned cross-process contracts.
- Station và inference đang nằm chung process/composition root.
- Chưa có ACK-after-durable-result/idempotency.
- Chưa có retry/DLQ/backpressure.

### High

- Identity/timestamp contract không khớp thiết kế.
- Exact inference crop chưa được freeze tại preparation boundary.
- State semantics đang gộp trong local result.
- Chưa có distributed trace/lifecycle timing.
- Secret redaction chưa bao phủ MinIO/Rabbit.
- Orientation ownership nằm ở script, không phải preparer boundary.

### Medium

- Old roadmap và Redis optional dependency có thể gây nhầm scope.
- Debug artifact writer và production spool dễ bị dùng nhầm nếu không tách namespace.
- Shutdown/reconnect phụ thuộc native FFmpeg/OpenCV, cần GX10 runtime verification.
- Result example trong design và string-valued extractor hiện tại có type tension.

## 12. Rủi ro triển khai

| Rủi ro | Hậu quả | Kiểm soát |
|---|---|---|
| Refactor pipeline làm lệch CV output | Regression accuracy/business | Compatibility façade + golden unit tests trước/sau |
| Reconstruct crop thay vì persist exact image | Worker OCR ảnh khác station đánh giá | Preparer trả immutable artifact payload/checksum |
| Publish trước artifact durability | Worker nhận job nhưng không có ảnh | Spool state + upload completion + publisher confirm ordering |
| ACK sớm | Mất job khi result chưa durable | Test ACK ordering và fault injection |
| Duplicate delivery | OCR lặp, result conflict | `event_id` idempotency + validate existing result |
| Disk full/backlog | Station mất evidence hoặc crash | Bounded thresholds, reject trigger, no auto-delete mặc định |
| Credential leakage | Security incident | Typed secret config, redaction tests, no raw URL in errors |
| ARM64/client incompatibility | GX10 runtime failure | Pin dependency và smoke ở 2C/2D trước E2E |
| Native camera shutdown | Process abort/hang | Giữ hardened close, kiểm tra riêng station lifecycle trên GX10 |
| Semantic mapping chưa xác nhận | False production claim | Preserve extractor, provenance profile/version, known blocker |

## 13. True blockers và quyết định hoãn

### Blocker để bắt đầu checkpoint 2A

**Không có blocker kỹ thuật thực sự.** Human pre-2A review đã được phê duyệt; có thể bắt đầu contracts/boundaries.

Đề xuất mặc định không làm thay đổi business:

- UUID: stdlib UUIDv4.
- Quality rejected: processing `COMPLETED`, business `REVIEW`.
- Extracted field payload: preserve current string-valued schema trong inner result.
- Spool cleanup: không tự xóa trong Phase 2 cho tới khi retention policy được duyệt.
- Retry schedule: config mặc định theo design `5,30,120` giây.

### Prerequisite trước runtime checkpoint 2C/2D

- Endpoint và credential test cho MinIO/RabbitMQ hoặc compose local được duyệt.
- Xác nhận image/client dependencies chạy trên GX10 ARM64.

Đây không chặn 2A/2B và không nên được hard-code vào repository.

### Known semantic blocker

`Nvidia P/N ↔ Customer Part Number ↔ Our Part Number` cần business confirmation. Phase 2 foundation phải preserve behavior hiện tại và ghi extractor/profile version trong provenance. Blocker này chặn claim production-verified cho `customer_part_number`, nhưng không chặn infrastructure implementation.

### Operational decisions chưa chốt

- Spool retention/cleanup.
- Disk/backlog alert thresholds cho deployment thật.
- Raw OCR retention duration.
- MinIO lifecycle policy.

Phase 2 có thể dùng fail-safe defaults và expose config, nhưng không được tự động xóa dữ liệu trước khi các policy này được duyệt.

## 14. Kết luận audit

Codebase hiện tại có nền CV local tốt để reuse: acquisition nền, bounded buffer, freshness/Top-K, orientation primitive, FixedROI, quality, resident OCR, ZXing, extractor và validator đều đã tồn tại. Khoảng trống chính không nằm ở model mà nằm ở process boundary, durable handoff, distributed contracts, state semantics và failure recovery.

Chiến lược ít rủi ro nhất là **tách boundary quanh logic hiện tại**, không rewrite CV core:

```text
existing camera/CV preparation
        → new durable spool/outbox
        → new MinIO + RabbitMQ transport
        → existing OCR/barcode/business logic inside a new resident worker
```

Sau khi bộ audit/plan này được duyệt, checkpoint đầu tiên phải là 2A contracts và boundaries; chưa được nhảy thẳng vào broker hoặc worker.
