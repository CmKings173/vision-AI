# Audit kiến trúc và khả năng triển khai Station/Worker

Ngày audit: 2026-08-28  
Snapshot source: `e0c8873` (`Add YOLO runtime acceptance and inspection evidence`)  
Phạm vi: đọc source, entrypoint, contract, packaging, systemd, Compose và tài liệu hiện có. Không chạy thay đổi production runtime và không refactor trong audit này.

## 1. Kết luận điều hành

Repository đã có nền tảng tách Station và Worker ở mức process và domain:

- `scripts/run_station.py` là entrypoint capture/preparation/outbox.
- `scripts/run_worker.py` là entrypoint OCR/barcode/extraction/validation.
- Camera và YOLO thuộc Station; PP-OCRv6 và ZXing thuộc Worker trong Phase 2.
- Handoff dùng bytes của `InspectionJob` qua RabbitMQ; ảnh/crop/result dùng `ArtifactRef` qua MinIO; không truyền Python object giữa hai process.

Tuy nhiên, hệ thống chưa đạt trạng thái deploy độc lập “clean” cho production. Điểm cản chính là `label_inspection.messaging.publisher` import trực tiếp `label_inspection.station.spool`; `Settings` vẫn là một cấu hình hợp nhất cho cả hai role; packaging chưa có profile/extras chính thức cho từng role; và repository chỉ có systemd unit cho Worker. Vì vậy verdict tổng thể là:

| Hạng mục | Verdict | Ý nghĩa |
|---|---|---|
| Code modularity | **PARTIAL** | Boundary logic đã có, nhưng còn import leak và compatibility façade. |
| Independent deployment | **PARTIAL** | Có process/entrypoint riêng, nhưng lifecycle, config, packaging và systemd chưa đủ độc lập. |
| Repository restructure | **SMALL_CLEANUP** | Không cần đổi layout lớn; cần tách transport adapter, role config/package và bổ sung service vận hành. |

Đây là kết luận từ source audit. Tài liệu hiện có cũng xác nhận real GX10 flow với MinIO, RabbitMQ, camera và YOLO vẫn là runtime acceptance/pending, không được suy ra là production-ready chỉ từ unit test hoặc model-load test.

## 2. Cây repository liên quan

```text
vision-AI/
├── scripts/
│   ├── run_station.py                 # Phase 2 Station entrypoint
│   ├── run_worker.py                  # Phase 2 Worker entrypoint
│   ├── manual_rtsp_inspection.py      # local synchronous POC
│   ├── inspect_image.py               # image POC/helper
│   ├── inspect_rtsp.py                # RTSP POC/helper
│   ├── run_real_image_integration.py  # runtime helper
│   ├── run_real_rtsp_integration.py   # runtime helper
│   ├── debug_yolo_detector.py         # detector-only debug helper
│   ├── test_ppocr_v6.py               # OCR runtime helper
│   └── test_zxing_runtime.py          # barcode runtime helper
├── src/label_inspection/
│   ├── app.py                         # factories: station, worker, POC
│   ├── config.py                      # one combined Settings object
│   ├── contracts/                     # InspectionJob/Result/ArtifactRef
│   ├── camera/                        # camera, acquisition, frame buffer
│   ├── detection/                     # FixedROI, contour, Ultralytics/YOLO
│   ├── preprocessing/                 # orientation, crop, quality, rectify
│   ├── pipeline/                      # ranking + synchronous POC façade
│   ├── station/                       # controller, preparation, spool, pump
│   ├── worker/                        # processor, inference worker, provenance
│   ├── messaging/                     # Rabbit topology, publisher, retry
│   ├── storage/                       # MinIO adapter, keys, deferred store
│   ├── ocr/                           # PP-OCR adapters
│   ├── barcode/                        # ZXing adapter
│   ├── extraction/                     # field extraction profiles
│   └── validation/                     # business validation rules
├── infra/phase2/                      # MinIO + RabbitMQ Compose only
├── ops/systemd/                       # Worker unit only
├── docs/                              # architecture and ADRs
├── pyproject.toml                     # package + optional extras
└── uv.lock                            # dependency lock
```

Các thư mục dữ liệu/untracked tại snapshot như `datasets/`, `dynamsoft/`, `phase1_input/`, `gx10_source_snapshot_7543efb_20260826/` không được xem là application deployable unit trong audit này.

## 3. Entrypoint và ranh giới Station

### 3.1 Entrypoint độc lập

| Entry point | Mục đích | Thành phần được khởi tạo | Đánh giá |
|---|---|---|---|
| `scripts/run_station.py:18-40` | Process capture/preparation/outbox | Camera, `FrameBuffer`, `StationPreparer`, `LocalSpool`, deferred MinIO, delivery pump, Rabbit publisher | **Độc lập ở cấp process; vận hành còn partial** |
| `scripts/run_station.py:64-149` | Compose Station runtime | `build_local_spool`, `build_station_preparer`, `RTSPCamera`, `CameraAcquisition`, `StationController`, `DeliveryPump` | **Đúng boundary Station** |
| `scripts/run_station.py:281-366` | Foreground loop | `service.start()`, `wait_ready()`, Enter trigger, stop/cleanup | **POC/operator loop; chưa phải daemon entrypoint hoàn chỉnh** |

`StationController.trigger()` tại `src/label_inspection/station/controller.py:64-104` tạo `TriggerEvent`, snapshot frame buffer, gọi `StationPreparer`, sau đó commit atomic vào `LocalSpool`. `StationPreparer` tại `src/label_inspection/station/preparation.py:130+` sở hữu selection, orientation, detector, crop/rectify và quality gate.

### 3.2 Những gì Station sở hữu

- mở/reconnect camera RTSP/HTTP;
- acquisition thread và bounded frame buffer;
- fresh-frame check, top-K selection và scoring;
- orientation normalization;
- FixedROI/contour/Ultralytics detector và YOLO checkpoint khi detector được chọn;
- crop/rectification, quality gate và lựa chọn candidate;
- local atomic spool, checksum, recovery và capacity backpressure;
- upload selected frame/label crop/job lên MinIO qua `OutboxDispatcher`;
- confirmed publish `InspectionJob` qua RabbitMQ;
- terminal result cho các trường hợp reject/error ngay tại preparation.

Station không gọi `build_processor()`, không warmup OCR, không decode barcode và không thực hiện `FieldExtractor`/`LabelValidator` trong Phase 2 path.

### 3.3 Station dependency leaks

1. `scripts/run_station.py:24-28` lấy profile/mapping constants từ `extraction.profiles`. Đây là dependency metadata hợp lý về mặt hiện tại, nhưng profile package hiện chứa logic extraction, chưa phải một package contract thuần.
2. `scripts/run_station.py:29-35` import cả `FrozenJobPublisher` và `PikaConfirmedPublisher` từ `messaging`. Transport publisher dùng được cho nhiều role, nhưng implementation hiện tại nằm chung với station spool publisher.
3. `src/label_inspection/app.py:12` import module `detection.ultralytics_detector` ở top level. Module này lazy-import package `ultralytics` trong constructor, nên Station fixed-ROI không load model; dù vậy đây vẫn là import surface cần giữ nhẹ nếu tách package thật.

Các dependency trên không khiến Station load OCR. Station có thể bắt đầu capture và ghi local spool khi MinIO/Rabbit chưa sẵn sàng; `DeliveryPump` chuyển trạng thái delivery sang degraded và retry nền.

## 4. Entrypoint và ranh giới Worker

### 4.1 Entrypoint độc lập

| Entry point | Mục đích | Thành phần được khởi tạo | Đánh giá |
|---|---|---|---|
| `scripts/run_worker.py:15-27` | Process inference | `build_processor`, `InferenceWorker`, MinIO store, Rabbit topology/publisher/retry handler | **Độc lập ở cấp process; lifecycle còn partial** |
| `scripts/run_worker.py:30-65` | Startup validation | `validate_phase2_worker`, MinIO connect/bucket validation, processor construction | Worker fail-fast nếu dependency/runtime chưa sẵn sàng |
| `src/label_inspection/worker/inference_worker.py:116-138` | Resident warmup | PP-OCR warmup và ZXing prepare | **Đúng ownership Worker** |
| `src/label_inspection/worker/inference_worker.py:140+` | Job processing | parse contract, download crop, verify checksum/decode, OCR+ZXing, extract, validate, persist result | **Đúng boundary Worker** |

`InspectionProcessor.process()` tại `src/label_inspection/worker/processor.py:21+` chạy OCR và barcode song song, sau đó extraction và validation. `InferenceWorker` không mở camera, không đọc frame buffer và không tạo YOLO detector.

### 4.2 Những gì Worker sở hữu

- consume RabbitMQ với manual ACK;
- parse/validate `InspectionJob`;
- kiểm tra `ArtifactRef`, giới hạn kích thước và namespace object;
- tải exact `label_crop` từ MinIO, verify SHA-256 và decode ảnh;
- resident PP-OCRv6/Transformers;
- resident ZXing/DataMatrix;
- field extraction và `LabelValidator`;
- tạo immutable `InspectionResult` và persist result vào MinIO;
- ACK sau khi xử lý thành công, retry/DLQ với `RetryingWorkerMessageHandler` tại `messaging/retry.py:64+`.

Worker không sở hữu camera, acquisition, trigger, ROI, crop, quality selection hay YOLO model trong production entrypoint.

### 4.3 Worker dependency leaks

Leak đáng kể nhất là chuỗi import sau:

```text
run_worker.py
  -> label_inspection.messaging
    -> messaging.publisher
      -> station.spool
        -> station.preparation
          -> camera.selector / detection / preprocessing / pipeline.types
```

Bằng chứng: `src/label_inspection/messaging/publisher.py:12` import `LocalSpool`, `RecordType`, `SpoolRecord` từ `..station.spool`. `FrozenJobPublisher` tại dòng 111+ cần dependency này cho Station, nhưng `PikaConfirmedPublisher` tại dòng 34+ chỉ là transport adapter và được Worker dùng tại `scripts/run_worker.py:17-25,68-74`.

Hậu quả hiện tại:

- Worker không instantiate camera hoặc StationPreparer, nên không làm sai runtime ownership.
- Worker vẫn bị ràng buộc source/package tới station spool và các dependency CV của Station.
- Không thể tuyên bố dependency isolation sạch; tối thiểu phải tách transport publisher khỏi spool publisher trước khi đóng gói hai role thành artifact độc lập.

`app.py` có top-level import class YOLO nhưng constructor `UltralyticsLabelDetector` mới import `ultralytics` và load checkpoint. Do đó đây là coupling/import surface, không phải bằng chứng Worker đang load YOLO.

## 5. Import dependency graph

```text
                          +----------------------+
                          | contracts            |
                          | Job / Result / Ref   |
                          +----------+-----------+
                                     ^
                 serialized bytes    | JSON + ArtifactRef
                                     |
+----------------------+             |             +----------------------+
| run_station.py       |             |             | run_worker.py        |
+----------+-----------+             |             +----------+-----------+
           |                         |                        |
           v                         |                        v
+----------------------+             |             +----------------------+
| app.build_station_   |             |             | app.build_processor  |
| preparer + spool     |             |             | OCR + ZXing + rules  |
+----------+-----------+             |             +----------+-----------+
           |                         |                        |
           v                         |                        v
+----------------------+             |             +----------------------+
| camera/acquisition   |             |             | worker/inference      |
| station/controller   |             |             | worker/processor     |
| station/preparation  |             |             +----------+-----------+
| station/spool        |             |                        |
+----------+-----------+             |                        |
           |                         |                        v
           |                         |             +----------------------+
           +--------> MinIO <--------+------------>| storage/key policy   |
           |                         |             +----------------------+
           +--------> RabbitMQ <-----+

Worker-only expected path: worker -> contracts/storage/ocr/barcode/extraction/
validation. Current accidental path: messaging.publisher -> station.spool.

Compatibility-only path:
manual_rtsp_inspection.py / inspect_* -> app.build_pipeline ->
pipeline.inspection -> StationPreparer + InspectionProcessor.
```

## 6. Process isolation và failure isolation

### 6.1 Process boundary

Boundary serialized hiện tại là đúng về nguyên tắc:

1. Station freezes job JSON trong `LocalSpool`.
2. `FrozenJobPublisher` gửi exact bytes, không rebuild job từ object mutable.
3. RabbitMQ chỉ vận chuyển bytes.
4. Worker gọi `InspectionJob.from_dict()` và kiểm tra contract/provenance.
5. Worker tải exact crop qua `ArtifactRef`, rồi reconstruct `PreparedInspection` nội bộ từ job/crop.
6. Worker ghi serialized `InspectionResult` immutable vào MinIO.

Không có shared in-memory object giữa hai process. Vì vậy OS/process isolation về data path là **PASS**. Tuy nhiên, lifecycle availability là **PARTIAL** do Worker fail-fast khi MinIO/Rabbit không sẵn sàng, không có reconnect loop trong entrypoint và chưa có Station service unit.

### 6.2 Ma trận lỗi

| Sự cố | Station | Worker | Hành vi hiện tại | Đánh giá |
|---|---|---|---|---|
| OCR không load | Capture/local commit vẫn chạy; job inference nằm trong spool/Rabbit | Không phát `WORKER_READY`, startup trả lỗi | Không làm hỏng Station | Tách process đạt; availability cần vận hành/restart |
| Worker crash giữa OCR | Không bị ảnh hưởng | Message chưa ACK được Rabbit requeue khi connection đóng; systemd restart nếu process exit | Không mất job theo thiết kế ACK | Cần test thật trên GX10 |
| Camera mất stream | Camera acquisition reconnect; trigger không ready nếu không có fresh frame | Worker vẫn có thể xử lý queue cũ | Không làm chết Worker | Đạt ở cấp process |
| RabbitMQ down | LocalSpool vẫn commit; `DeliveryPump` degraded/retry | Worker không connect/consume hoặc exit | Capture có local durability | Đúng hướng, nhưng cần alert/retention |
| MinIO down | LocalSpool vẫn commit; delivery retry | Startup `validate_bucket` fail hoặc job không tải/persist | Station chịu được mất tạm thời tốt hơn Worker | Lifecycle chưa đồng nhất |
| Worker restart | Không ảnh hưởng camera/spool | systemd `Restart=on-failure`; unacked message được trả lại broker | Có retry ở broker/handler | Unit còn thiếu stop/readiness hardening |
| Rabbit publish failure trong Worker callback | Message hiện tại để unacked và dừng consume | Process kết thúc; systemd có thể restart | Tránh mất message nhưng throughput dừng | Cần policy reconnect rõ ràng |
| Rabbit connect failure trong `run_worker.py` | Không ảnh hưởng Station | Trả lỗi trước `finally`; connection mở một phần không có cleanup đối xứng | Có rủi ro resource leak trong startup lỗi | Blocker reliability nhỏ nhưng thật |

## 7. Contract boundary và Rabbit/MinIO

### 7.1 Contract

Contract public nằm trong `src/label_inspection/contracts/`:

- `core.py:36+`: `DeliveryStatus`, `ProcessingStatus`, `BusinessStatus`, `ArtifactRef` và error types;
- `job.py:23+`: `JOB_SCHEMA_VERSION = "inspection-job.v1"`, immutable `InspectionJob`, `to_dict/from_dict`;
- `result.py:23+`: `RESULT_SCHEMA_VERSION = "inspection-result.v1"`, immutable terminal `InspectionResult`, `to_dict/from_dict`;
- `contracts/__init__.py`: public export boundary.

Điểm tốt:

- schema version rõ;
- identity/event fields và artifact checksum được kiểm tra;
- `ArtifactRef` có bucket/key/content-type/size/SHA-256;
- job/result đi qua JSON bytes, không phụ thuộc class implementation của process còn lại;
- result persist idempotent/immutable và có readback/integrity checks ở storage layer.

Điểm chưa sạch:

- internal `PreparedInspection` vẫn là type chung giữa station preparation và worker processor, dù qua distributed path Worker reconstruct lại type này từ job/crop;
- `messaging.publisher` trộn transport adapter với Station outbox publisher;
- `storage.keys.ArtifactKeyPolicy` hiện là shared storage policy có logic worker validation, nên nên được coi là contract/storage boundary chứ không phải domain của riêng Worker.

Verdict contract boundary: **PARTIAL**. Wire contract tốt; implementation dependency boundary chưa clean.

### 7.2 RabbitMQ

- `src/label_inspection/messaging/topology.py` định nghĩa exchange, queue, retry TTL/DLQ và prefetch.
- `PikaConfirmedPublisher` dùng persistent message, publisher confirms và mandatory routing.
- Station dùng `FrozenJobPublisher` để chỉ advance `ARTIFACTS_READY -> JOB_PUBLISHED` sau confirm.
- Worker dùng `RetryingWorkerMessageHandler` để ACK sau result/retry/DLQ.

RabbitMQ chỉ nên là transport của `InspectionJob` bytes. Hiện tại transport class bị kéo qua Station spool vì code organization, không phải do wire protocol.

### 7.3 MinIO

- `src/label_inspection/storage/minio_store.py:37+` là shared S3-compatible adapter.
- `put_if_absent` tại dòng 130+ hỗ trợ conditional immutable write và verify conflict/readback.
- `get_verified` tại dòng 161+ giới hạn read và verify integrity.
- Station dùng `DeferredArtifactStore` tại `storage/deferred.py:12+` để local capture không phụ thuộc synchronous MinIO availability.
- Worker dùng direct `MinioArtifactStore` và `ArtifactKeyPolicy` để tải crop, sau đó persist result.
- `storage/keys.py:27+` và `:111+` quy định namespace object key deterministic.

### 7.4 Compose hiện có

`infra/phase2/docker-compose.yml` chỉ chạy:

- MinIO image pinned, named volume, localhost bind và bootstrap bucket/app policy;
- RabbitMQ image pinned, named volume, healthcheck và localhost bind.

`infra/phase2/README.md:10-12,61-113` nói rõ Station và Worker là native process trên GX10, Compose không chứa application container và không có MediaMTX. Đây là lựa chọn phù hợp cho foundation/GX10 acceptance, nhưng chưa phải một deployment artifact hoàn chỉnh cho hai role.

## 8. Configuration isolation

`src/label_inspection/config.py:82` định nghĩa một `Settings` dataclass chứa đồng thời:

| Nhóm | Ví dụ |
|---|---|
| Station | `station_id`, `camera_id`, `rtsp_url`, spool limits, buffer/window, RTSP timeout, rotation, ROI, detector, YOLO model/device/thresholds, quality weights |
| Shared transport/storage | `artifact_bucket`, MinIO endpoint/credential, Rabbit URL, dispatch interval, object/message limits, retry delays |
| Worker | OCR engine/backend/version/device/path/input/threshold, barcode engine, extraction profile, required fields, barcode policy |
| Logging | log level |

Validation được tách một phần:

- `validate_station()` tại `config.py:296+` kiểm tra camera/detector/preparation;
- `validate_worker()` tại `config.py:398+` kiểm tra OCR/barcode/profile;
- `validate_phase2_transport()` tại `config.py:451+` kiểm tra MinIO/Rabbit/limits/retry;
- `validate_phase2_station()` tại `:502+` gọi station + transport;
- `validate_phase2_worker()` tại `:506+` gọi worker + transport và yêu cầu PP-OCRv6.

Nhưng cả hai entrypoint vẫn gọi `Settings()` và đọc cùng một environment namespace. `infra/phase2/.env.example` cũng trộn camera/ROI/YOLO với OCR/ZXing/MinIO/Rabbit trong một file. Vì vậy:

- thiếu OCR settings không làm Station validation fail, nhưng config object Station vẫn mang chúng;
- thiếu camera/ROI không làm Worker logic validation fail, nhưng Worker vẫn tạo cùng Settings object;
- secret/endpoint chung có thể dùng, còn role-specific config chưa được hard boundary.

Verdict config isolation: **PARTIAL**. Khuyến nghị tương lai là `SharedInfrastructureSettings`, `StationSettings`, `WorkerSettings` hoặc role-specific views được tạo từ env, không cần phá contract.

## 9. Dependency và packaging isolation

`pyproject.toml` hiện có base dependency:

- `numpy`, `opencv-python-headless`, `python-dotenv`.

Optional extras hiện có:

| Extra | Dependency | Role dự kiến |
|---|---|---|
| `detector` | `torch`, `ultralytics` | Station khi dùng YOLO |
| `ocr-transformers` | `paddleocr`, `transformers` | Worker PP-OCRv6 Transformers |
| `barcode` | `zxing-cpp` | Worker |
| `phase2` | `minio==7.2.20`, `pika==1.4.4` | Shared transport/storage |
| `ocr` / `ocr-tensorrt` | OCR runtime variants | Worker runtime variants |
| `dev` | pytest | Development |

`uv.lock` có entries cho MinIO, Pika, PaddleOCR, Transformers, Ultralytics và zxing-cpp; lock coverage là tốt hơn việc không lock, nhưng không có `station`/`worker` extras hoặc image/requirements riêng. `pyproject.toml` cũng không khai báo console scripts; deployment phải gọi trực tiếp `/opt/vision-AI/.venv/bin/python scripts/run_*.py`.

Vì vậy có thể dựng env tối giản thủ công, nhưng repository chưa cung cấp hai install contract rõ ràng kiểu:

```text
station = base + detector(optional) + phase2
worker  = base + ocr-transformers + barcode + phase2
```

Đây là dependency isolation **PARTIAL**. Worker import leak qua `messaging.publisher` làm vấn đề này nghiêm trọng hơn dù YOLO package thực tế không được load trong Worker.

## 10. Model ownership

| Runtime/model | Owner Phase 2 | Bằng chứng |
|---|---|---|
| FixedROI | Station | `app.py:130+`, `station/preparation.py` |
| Contour heuristic | Station, experimental | `app.py:130+` |
| Ultralytics/YOLO checkpoint | Station | `app.py:140+`, `run_station.py:67-68`; warmup/inference qua `StationPreparer` |
| PP-OCRv6 Transformers | Worker | `app.py:50-106`, `run_worker.py:41-53`, `InferenceWorker.start()` |
| ZXing/DataMatrix | Worker | `app.py:57`, `InspectionProcessor`, `InferenceWorker.start()` |
| FieldExtractor/LabelValidator | Worker | `app.py:50-106`, `worker/processor.py` |

Kết luận trực tiếp:

- `STATION_LOADS_OCR`: **NO** trong production Phase 2 entrypoint.
- `WORKER_LOADS_YOLO`: **NO** trong production Phase 2 entrypoint.
- `manual_rtsp_inspection.py` là ngoại lệ POC: `build_pipeline()` cố ý lắp cả detector, OCR và barcode trong một process.

YOLO hiện vẫn được đánh dấu `EXPERIMENTAL` trong `detection/ultralytics_detector.py`; ADR-002 yêu cầu live acceptance có detection-attempt evidence trước khi promotion.

## 11. Deployment và systemd

### 11.1 Station

Hiện **không có** `ops/systemd/vision-station.service`. `run_station.py` chạy foreground, chờ `input()` để trigger và tự stop khi Ctrl-C. Điều này hữu ích cho acceptance/operator test nhưng chưa phải service contract cho boot/restart/logging/health.

Station vẫn có hành vi tốt khi delivery dependency mất: capture/local spool được khởi động trước; MinIO/Rabbit được pump nền. Đây là lý do Station process boundary có giá trị ngay cả khi infrastructure chưa ready.

### 11.2 Worker

`ops/systemd/vision-inference-worker.service:1-17` có:

- `User=vision`;
- `WorkingDirectory=/opt/vision-AI`;
- `EnvironmentFile=/etc/vision-ai/worker.env`;
- venv `ExecStart` tới `scripts/run_worker.py`;
- `Restart=on-failure`, `RestartSec=5`;
- `NoNewPrivileges=true`.

Unit là một base unit dùng được, nhưng verdict là **PARTIAL** vì:

- không có `TimeoutStopSec`, `KillSignal` hoặc shutdown guarantee cho broker/worker;
- không có readiness/health integration ngoài stdout;
- không có provisioning file cho user `vision`, model path, permissions, venv và env file;
- Worker fail-fast khi MinIO/Rabbit startup chưa sẵn sàng và không có reconnect loop trong process;
- nhánh Rabbit connect lỗi tại `run_worker.py:67-86` không cleanup connection mở một phần;
- không có unit đối xứng cho Station.

### 11.3 Infrastructure

Compose local/GX10 hiện phù hợp cho MinIO + RabbitMQ foundation: image/version pin, named volume, localhost bind, bootstrap bucket/app policy. Nó không triển khai app process, không có HA, không có secret manager, scheduler, central metrics/tracing hay multi-station coordination. Đây là **PARTIAL / acceptance foundation**, không phải production HA package.

## 12. POC và production separation

| Nhóm | Script/path | Có được production entrypoint gọi không? | Nhận xét |
|---|---|---:|---|
| Phase 2 Station | `scripts/run_station.py` | Có | Capture/preparation/outbox only |
| Phase 2 Worker | `scripts/run_worker.py` | Có | OCR/barcode/business inference |
| Local synchronous POC | `scripts/manual_rtsp_inspection.py` | Không | `build_pipeline()`, một process, detector + OCR + barcode |
| Image/RTSP helpers | `scripts/inspect_image.py`, `inspect_rtsp.py`, `run_real_*` | Không | Manual/runtime evidence |
| Detector/OCR/barcode debug | `debug_yolo_detector.py`, `test_ppocr_v6.py`, `test_zxing_runtime.py` | Không | Diagnosis/runtime checks |
| Compatibility façade | `pipeline/inspection.py` | Chỉ qua POC/evaluation | Đã ghi rõ không dùng bởi async Station/Worker |

POC/production separation: **PASS ở mức entrypoint**. Cần giữ `build_pipeline()` như compatibility path có đánh dấu rõ; không nên dùng nó làm factory cho deployment distributed vì nó cố ý load cả hai domain.

## 13. Ma trận deployment readiness

| Capability | Trạng thái | Evidence | Khoảng trống |
|---|---|---|---|
| Station capture + fresh frame | READY về code | `run_station.py`, `StationService`, `RTSPCamera` | Cần live camera/network acceptance |
| Station local durability | READY về code/test local | `station/spool.py`, atomic records/recovery | Cần failure matrix thật trên GX10 |
| Station YOLO ownership | READY về boundary; EXPERIMENTAL về support | `build_station_preparer`, ADR-002 | Chưa đủ live 50-attempt/promote evidence |
| Worker OCR/ZXing ownership | READY về boundary | `build_processor`, `InferenceWorker.start` | Cần runtime model/device validation trên target |
| Rabbit job contract | READY về schema/code | `inspection-job.v1`, topology/retry | Real confirm/TTL/DLQ/reconnect còn pending |
| MinIO artifact contract | READY về adapter/code | conditional put/get verified | Real service/credential/permission acceptance còn pending |
| Station ↔ Worker process isolation | PARTIAL | bytes + MinIO/Rabbit boundary | Worker import leak và lifecycle hardening |
| Config isolation | PARTIAL | validation tách method | Một `Settings`, một env template cho mọi role |
| Dependency isolation | PARTIAL | optional extras + lazy runtime imports | `messaging.publisher -> station.spool`, chưa có role extras |
| Station service management | NOT_READY | không có unit | Cần station systemd/daemon mode |
| Worker service management | PARTIAL | worker unit có Restart | Cần stop/readiness/provisioning/reconnect hardening |
| Native package/image deployment | PARTIAL | pyproject + uv.lock | Chưa có install profile/image riêng cho role |
| HA/central observability | NOT_READY | không có trong tree hiện tại | Ngoài scope Phase 2 foundation |

## 14. Top deployment blockers

1. **Worker import leak:** `messaging.publisher` import `station.spool`, kéo Station implementation vào Worker package và phá dependency isolation sạch.
2. **Thiếu Station systemd/daemon contract:** chỉ có worker unit; Station hiện là foreground `input()` loop, chưa có boot/restart/log/stop semantics cho deployment.
3. **Config chưa role-isolated:** một `Settings` và một `.env.example` chứa camera, YOLO, OCR, barcode, MinIO và Rabbit; khó cấp quyền và đóng gói độc lập.
4. **Packaging chưa có role profile:** optional extras tồn tại nhưng chưa có `station`/`worker` install contract, image hoặc provisioning artifact tương ứng.
5. **Worker lifecycle/runtime acceptance chưa đủ:** Worker fail-fast khi MinIO/Rabbit chưa sẵn sàng, chưa có reconnect/readiness hardening; real GX10 E2E, YOLO live acceptance, Rabbit confirm/DLQ và MinIO conditional-write vẫn là acceptance gates.

## 15. Thay đổi tối thiểu được khuyến nghị (không thực hiện trong audit)

1. Tách `PikaConfirmedPublisher`/transport interface sang module shared không import Station; giữ `FrozenJobPublisher` ở Station boundary. Đây là thay đổi quan trọng nhất.
2. Tạo role-specific configuration views hoặc dataclasses (`SharedInfrastructureSettings`, `StationSettings`, `WorkerSettings`) từ cùng env source; không để Worker nhận camera/ROI và không để Station nhận OCR secrets như dependency bắt buộc.
3. Thêm `station` và `worker` packaging/install profiles, hoặc hai lock/install manifests có kiểm soát; giữ `uv.lock` làm nguồn khóa dependency chung.
4. Thêm `ops/systemd/vision-station.service` với mode trigger phù hợp cho daemon; bổ sung timeout/stop/readiness semantics cho Worker unit.
5. Bổ sung real acceptance trên GX10 cho: camera loss, MinIO/Rabbit loss, worker crash/restart, retry/DLQ, result idempotency, YOLO detection attempts và permission/model-path của user service.

Không cần wholesale repository restructure. Layout `station/`, `worker/`, `contracts/`, `storage/`, `messaging/` hiện đã đủ gần mục tiêu; chỉ cần làm sạch shared transport/config/package boundary.

## 16. Cấu trúc mục tiêu tối thiểu

Đây là cấu trúc khái niệm sau cleanup, không phải yêu cầu di chuyển toàn bộ repository:

```text
src/label_inspection/
├── contracts/                 # wire contracts, schema versions, ArtifactRef
├── shared/
│   ├── config/                # shared transport + role views
│   └── transport/             # PikaConfirmedPublisher, topology interfaces
├── station/
│   ├── controller.py
│   ├── preparation.py
│   ├── spool.py
│   ├── dispatcher.py
│   └── service.py
├── worker/
│   ├── inference_worker.py
│   ├── processor.py
│   └── provenance.py
└── storage/
    ├── minio_store.py
    ├── deferred.py
    └── keys.py
```

`FrozenJobPublisher` có thể ở `station/` và gọi interface transport từ `shared/`; Worker chỉ import transport interface/adapter, contracts, storage và worker inference.

## 17. Verdict cuối cùng

- **CODE_MODULARITY:** PARTIAL
- **INDEPENDENT_DEPLOYMENT:** PARTIAL
- **REPOSITORY_RESTRUCTURE_REQUIRED:** SMALL_CLEANUP
- **STATION_ENTRYPOINT:** `scripts/run_station.py` — independent process boundary, operational readiness partial
- **WORKER_ENTRYPOINT:** `scripts/run_worker.py` — independent process boundary, dependency/lifecycle readiness partial
- **STATION_SYSTEMD:** NO
- **WORKER_SYSTEMD:** PARTIAL
- **STATION_LOADS_OCR:** NO
- **WORKER_LOADS_YOLO:** NO
- **CONFIG_ISOLATION:** PARTIAL
- **DEPENDENCY_ISOLATION:** PARTIAL
- **CONTRACT_BOUNDARY:** PARTIAL
- **PROCESS_ISOLATION:** PARTIAL (OS process/data boundary đạt; lifecycle availability chưa đủ)
- **POC_PRODUCTION_SEPARATION:** PASS
- **NO_RUNTIME_CODE_MODIFIED:** YES
