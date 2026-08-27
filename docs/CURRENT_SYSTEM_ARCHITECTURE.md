# Vision-AI — Current System Architecture

> **Tài liệu source of truth cho trạng thái hiện tại của repository**
>
> Audit snapshot: `HEAD 24632b57212cdde25394b2ef5679ea3e32e28d53`
>
> Phạm vi: đọc source, contracts, tests, scripts, deployment và tài liệu hiện có; không thay đổi implementation. Mọi mô tả bên dưới ưu tiên hành vi trong source hiện tại. Những gì chỉ có trong kế hoạch, comment hoặc runtime evidence cũ được đánh dấu rõ là giới hạn/chưa xác minh.

## 1. Mục tiêu hệ thống

Vision-AI là hệ thống kiểm tra nhãn vận chuyển bằng computer vision. Đầu vào là ảnh đơn hoặc luồng camera RTSP/HTTP; đầu ra là quyết định nghiệp vụ `PASS`, `REVIEW` hoặc `FAIL`, kèm OCR raw text, các business fields, barcode/DataMatrix, quality observations, timing và artifact evidence.

Mục tiêu hiện tại gồm hai lớp:

1. **Local synchronous POC** để kiểm tra nhanh ảnh/RTSP bằng một process. Nó giữ frame trong memory, nhận manual trigger, chạy pipeline và in JSON/debug artifacts.
2. **Phase 2 production foundation** để tách station capture/preparation khỏi worker inference bằng Local Spool, MinIO và RabbitMQ. Đây là nền tảng phân tách durability/delivery, chưa phải hệ thống HA hoặc production acceptance hoàn chỉnh.

Hệ thống không tự suy đoán quan hệ business giữa `Nvidia P/N`, `Customer Part Number` và `Our Part Number`. Production extractor hiện vẫn có alias `NVIDIA P/N` vào `customer_part_number`, nhưng mapping này được ghi là `KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION` và chưa được coi là production-verified trong đánh giá.

## 2. Phạm vi hiện tại và trạng thái

| Capability | Trạng thái | Bằng chứng / giới hạn |
|---|---|---|
| Đọc ảnh local | IMPLEMENTED | `scripts/inspect_image.py`, `RTSPCamera`/video helpers |
| Camera RTSP/HTTP qua OpenCV/FFmpeg | IMPLEMENTED/POC | `camera/rtsp.py`; cần camera/network thực tế |
| Continuous acquisition | IMPLEMENTED | `CameraAcquisition` daemon thread |
| Ring buffer bounded | IMPLEMENTED | `FrameBuffer`, mặc định 8 frame/800 ms |
| Manual Enter/timed trigger | IMPLEMENTED/POC | `manual_rtsp_inspection.py`, `run_real_rtsp_integration.py` |
| Fresh-frame check | IMPLEMENTED | monotonic age/window; stale được trả về như lỗi kỹ thuật |
| Top-K selection | IMPLEMENTED | top-K tối đa 3, score trên preview |
| Fixed ROI | SUPPORTED | default/configurable; manual test từ chối full-frame ROI |
| Contour detector | EXPERIMENTAL | heuristic contour, không phải production detector |
| Ultralytics/YOLO detector | EXPERIMENTAL | đã tích hợp interface/runtime metadata; acceptance live chưa hoàn tất |
| Crop/optional rectify | IMPLEMENTED | `Cropper`, corners rectify nếu candidate có corners |
| Quality gate | IMPLEMENTED/PROVISIONAL | threshold hiện tại chưa camera-calibrated |
| Local atomic spool | IMPLEMENTED/TESTED LOCALLY | fsync/atomic rename/path containment; real Linux failure matrix chưa hoàn tất |
| PP-OCRv6 DET+REC | IMPLEMENTED | resident adapter, startup load/warmup |
| ZXing/DataMatrix | IMPLEMENTED | nhiều preprocessing variants, dedup và chọn result |
| Field extraction bằng regex | IMPLEMENTED | không dùng LLM |
| Label validation | IMPLEMENTED | field confidence/missing/quality/barcode rules |
| MinIO artifact delivery | IMPLEMENTED LOCALLY / RUNTIME PENDING | private conditional PUT chưa real-runtime verified |
| Rabbit confirmed publish/retry/DLX | IMPLEMENTED LOCALLY / RUNTIME PENDING | real confirms/TTL/DLX/reconnect chưa được acceptance |
| Worker process | IMPLEMENTED | `run_worker.py`, có systemd unit |
| Station process | IMPLEMENTED | `run_station.py`, hiện chạy foreground; chưa có station systemd unit |
| Central metrics/trace | NOT IMPLEMENTED | chỉ structured stdout, timing và report local |
| Industrial camera SDK | NOT IMPLEMENTED | chưa có USB/GigE adapter cụ thể |
| HA/multi-station orchestration | NOT IMPLEMENTED | không có Kubernetes/HA control plane |

## 3. Kiến trúc tổng thể

### 3.1 Local synchronous POC

```text
[Android phone / RTSP or HTTP]
          |
          v
 [RTSPCamera: OpenCV/FFmpeg]
          |
          v
 [CameraAcquisition daemon]
          |
          v
 [bounded FrameBuffer in memory]
          |
   Enter / timed trigger
          |
          v
 [snapshot -> Top-K FrameSelector]
          |
          v
 [orientation -> FixedROI or YOLO -> crop/rectify]
          |
          v
 [quality gate -> best candidate]
          |
          +--------------------+
          |                    |
          v                    v
   [PP-OCRv6 DET+REC]   [ZXing barcode/DataMatrix]
          |                    |
          +---------+----------+
                    v
          [FieldExtractor -> LabelValidator]
                    |
                    v
        [InspectionResult + debug artifacts]
```

Entrypoint POC chính là `scripts/manual_rtsp_inspection.py`. `build_pipeline()` trong `src/label_inspection/app.py:109` lắp detector, preparer và processor; pipeline đồng bộ tại `src/label_inspection/pipeline/inspection.py:29`.

### 3.2 Phase 2 distributed foundation

```text
                       GX10 host
  +---------------------------------------------------------------+
  | Native process: station                                      |
  |                                                               |
  | Camera -> RingBuffer -> Trigger -> Preparation                |
  | (orientation/detector/crop/quality/top candidate)             |
  |              |                                                |
  |              v                                                |
  |       LocalSpool atomic record                                |
  |              |                                                |
  |       background dispatcher                                   |
  |          | upload artifacts                                   |
  |          | confirmed publish                                  |
  |          v                                                    |
  |  +-------------------- Docker Compose ---------------------+  |
  |  | MinIO (artifact store)   RabbitMQ (job transport)        |  |
  |  +---------------------------------------------------------+  |
  |              |                                 |              |
  |              +---------------+-----------------+              |
  |                              v                                |
  | Native process: worker                                        |
  | Rabbit consume -> validate job -> get label_crop from MinIO   |
  | -> (PP-OCRv6 || ZXing) -> extraction -> validation            |
  | -> put result -> readback verify -> durable result state      |
  +---------------------------------------------------------------+
```

Station không chạy OCR/ZXing trong Phase 2 preparation path. Worker không đọc camera; nó chỉ xử lý `InspectionJob` và artifact đã được station commit/upload. Local POC và Phase 2 là hai execution path khác nhau, không nên dùng kết quả của một path để khẳng định path kia đã acceptance.

## 4. Runtime và deployment topology

- Camera điện thoại nằm ngoài GX10, truyền RTSP/HTTP qua network.
- Station và worker hiện là native Python processes trên GX10, dùng virtual environment của repository.
- MinIO và RabbitMQ được cung cấp bởi `infra/phase2/docker-compose.yml`, bind vào loopback của host theo mặc định: MinIO API `127.0.0.1:9000`, console `127.0.0.1:9001`, Rabbit AMQP `127.0.0.1:5672`, management `127.0.0.1:15672`.
- Compose chỉ chạy infrastructure; không có app container, MediaMTX hoặc camera gateway.
- MinIO dùng named volume `vision-ai-phase2-minio-data`; RabbitMQ dùng `vision-ai-phase2-rabbitmq-data`.
- `ops/systemd/vision-inference-worker.service` quản lý worker native với `Restart=on-failure`. Hiện không có station systemd unit tương ứng.
- Mô hình này phù hợp dev/GX10 single-host acceptance. Chưa có HA, scheduler, multi-station coordinator, centralized secret manager hoặc failover node.
- Tài liệu/runtime có evidence POC trước đây trên phone camera, nhưng full production E2E với Docker services, actual conditional PUT, real Rabbit confirm/DLX và worker/station recovery vẫn là `RUNTIME PENDING`.

## 5. Camera layer

`CameraSource` protocol tại `src/label_inspection/camera/base.py:15` định nghĩa `open()`, `read()`, `frames()` và `close()`. `RTSPCamera` tại `camera/rtsp.py:30` là implementation hiện tại:

- Dùng OpenCV, có thể chọn FFmpeg backend.
- `CAP_PROP_BUFFERSIZE=1` để giảm trễ phía capture nếu backend hỗ trợ.
- Có timeout mở/đọc, detach/release khi lỗi và backoff reconnect.
- Mỗi `FramePacket` có `frame_id`, wall-clock `captured_at`, monotonic `captured_monotonic`, source và numpy frame.
- Camera source timestamp đáng tin cậy chưa có; timestamp hiện là thời điểm GX10 nhận frame.
- `CameraAcquisition` chạy read loop trong daemon thread, append packet vào `FrameBuffer`, cập nhật `frames_received`, last error và health.
- Release native capture được trì hoãn nếu đang có read lock; `wait_closed()` hỗ trợ shutdown tránh FFmpeg abort race.

Đổi sang camera công nghiệp không chỉ là đổi URL. Cần adapter thực hiện `CameraSource`, mapping exposure/trigger/timestamp/pixel format và test lại freshness/latency. Comment có nhắc USB/GigE extension point nhưng source chưa có implementation SDK công nghiệp.

## 6. Trigger và inspection lifecycle

### Local POC

1. Process khởi tạo detector/OCR/ZXing và warmup.
2. Mở camera, acquisition liên tục đẩy frame vào buffer.
3. Người vận hành nhấn Enter hoặc timed trigger.
4. Process lấy snapshot các frame còn fresh, tạo inspection event, chọn Top-K.
5. Candidate tốt nhất đi qua detection/crop/quality rồi inference.
6. Kết quả JSON được in; tùy option lưu selected frame, label crop và `result.json` theo event.

POC hiện tạo event id dạng `INS-` + 12 ký tự hex trong `scripts/manual_rtsp_inspection.py`. Đây là ID hợp lệ cho local schema nhưng **không phải canonical UUIDv4**.

### Phase 2

`StationController.trigger()` tạo `TriggerEvent` bằng `TriggerEvent.create()` tại `contracts/core.py:161`, gồm canonical `event_id`, `trigger_id`, `station_id`, `camera_id`, `triggered_at_ms`. Controller snapshot buffer, preparation và commit Local Spool. Nếu cần inference, dispatcher upload/publish sau đó; worker xử lý bất đồng bộ.

## 7. Freshness, frame selection và Top-K

`FrameBuffer` tại `camera/frame_buffer.py:13` là deque bounded, mặc định `maxlen=8`, `window_ms=800`. Snapshot lọc theo monotonic time nếu packet có monotonic timestamp; nếu không thì dùng wall-clock. `max_frame_age_ms` mặc định là 1000 ms.

`FrameSelector` tại `camera/selector.py:76`:

- lấy tối đa `top_k=3` packet;
- tính score trên preview downsample có long edge mặc định 480;
- score gồm quality preview và tie-break freshness rất nhỏ;
- chỉ giữ packet/frame đầy đủ, không dùng preview làm input inference;
- sort giảm dần và trả Top-K để preparation thử tiếp.

Candidate cuối cùng được `CandidateScorer` tại `pipeline/ranking.py:42` xếp hạng từ detection, sharpness, exposure, area, freshness, glare và validity. Trọng số là heuristic cấu hình, chưa được calibration trên dataset/camera production.

Do frame được lấy liên tục, “ảnh tốt nhất” chỉ có nghĩa là ảnh có score cao nhất trong cửa sổ snapshot theo các metric hiện tại; không phải ground-truth đảm bảo OCR đúng. Quality score và detector confidence không chứng minh field correctness.

## 8. Label localization/detection: FixedROI và YOLO

`LabelDetector` protocol ở `detection/base.py:10` cho phép thay detector mà không đổi downstream contract.

### FixedROI

`FixedROIDetector` tại `detection/fixed_roi.py:35` nhận normalized `(x1,y1,x2,y2)`, kiểm tra finite và quan hệ tọa độ, clamp vào ảnh rồi trả một candidate. Đây là đường ổn định cho camera/pose cố định. Manual POC không chấp nhận `0,0,1,1` như acceptance ROI vì đó là full-frame, không chứng minh crop đúng label.

### Contour

`ContourDetector` là heuristic experimental dựa trên grayscale/Otsu/contours, trả tối đa 5 candidate. Không có bằng chứng production acceptance.

### Ultralytics/YOLO

`UltralyticsLabelDetector` tại `detection/ultralytics_detector.py:20`:

- load checkpoint cụ thể, kiểm tra file tồn tại và SHA-256;
- map `gpu` về `cuda:0` và xác minh torch device thực tế;
- yêu cầu class mapping có `shipping_label`;
- warmup resident một lần;
- inference theo `device`, `imgsz`, `conf`, `iou`, `max_det`;
- lưu raw/accepted detections và runtime metadata.

YOLO chỉ định vị label, không đọc chữ và không thay thế PP-OCRv6. Source đánh dấu detector này `EXPERIMENTAL`; một image/GX10 POC pass trước đây không tương đương live 50-attempt production acceptance.

## 9. Crop và quality gate

Preparation tại `station/preparation.py:174` thực hiện orientation normalization, detector, crop với `bbox_padding_ratio` mặc định `.05`, optional corner rectification, rồi quality check.

`measure_quality`/`QualityChecker` tại `preprocessing/quality.py:10` tính:

- kích thước và area;
- mean brightness;
- underexposed ratio (`gray <= 30`);
- overexposed ratio (`gray >= 245`);
- glare ratio (`gray >= 252`);
- Laplacian variance/sharpness.

Default quality thresholds hiện tại: min width 32, min height 16, min sharpness 50, brightness `[20,245]`, max underexposed `.30`, overexposed `.30`, glare `.20`. Chúng là threshold runtime/config hiện tại, chưa camera-calibrated.

Nếu quality fail nhưng observation hợp lệ, station có thể trả terminal `COMPLETED/REVIEW` và không chạy inference. Nếu quality observation lỗi/không hợp lệ thì technical error. Khi quality pass, label crop mới là input cho OCR và ZXing; full frame chỉ là evidence/selection context.

## 10. Local Spool và durability

`LocalSpool` tại `station/spool.py:57` là boundary durability trên filesystem local. Nó kiểm tra pending count/bytes/free disk trước commit; default lần lượt khoảng 1000 event, 10 GiB và 2 GiB free.

Inference record có layout:

```text
spool/<canonical-event-uuid>/
  selected_frame.jpg
  label_crop.png
  job.json
  state.json
```

Terminal quality/error record có thể chứa selected/crop nếu có, `result.json` và `state.json`; không tạo inference job nếu inference không cần.

Commit dùng thư mục tạm `.tmp_<event_id>`, exclusive file creation, flush + fsync từng file, fsync temp directory, `os.replace()` cùng filesystem và fsync root directory. Windows directory fsync là best-effort; POSIX failure được xử lý nghiêm hơn.

`open_record()` kiểm tra state, manifest hash/size/content, contract, identity, containment và symlink. `scan_recovery()` chỉ report pending/delivered/corrupt/incomplete; không tự xóa dữ liệu. Record delivered vẫn còn để audit nhưng không tính vào pending capacity. Retention/deletion policy tự động chưa được implement.

Atomic rename giả định temp và final cùng filesystem; policy này chưa được biểu diễn thành cấu hình riêng. Backpressure và free-disk limit vẫn là invariant khi delivery backend unavailable.

## 11. InspectionJob contract

Contract Phase 2 tại `contracts/job.py:27` là strict `inspection-job.v1`:

| Nhóm | Nội dung |
|---|---|
| Identity | canonical UUID `event_id`, `trigger_id`; `station_id`, `camera_id` |
| Time | `triggered_at_ms`, `received_at_ms`, optional `source_timestamp_ms`, `prepared_at_ms`, `created_at_ms` |
| Processing | job phải ở `PREPARED` trước khi publish |
| Selection | selected frame id, locator/crop/selection information |
| Quality | quality report/observation và preparation timing |
| Artifacts | `ArtifactRef` cho ít nhất label crop, kèm selected/source evidence nếu có |
| Provenance | detector, extractor/profile, config/runtime identity và stage information |
| Schema discipline | unknown/missing fields bị reject bởi `from_dict()` |

Job được freeze thành exact `job.json` trong spool. Publisher phải gửi đúng bytes đó; worker không tự thay đổi payload business.

Local `schemas.py:210` có `InspectionResult` khác với Phase 2 contract. Không được coi hai JSON shape là interchangeable.

## 12. ArtifactRef và MinIO

`ArtifactRef` ở `contracts/core.py:200` chứa bucket, key, SHA-256, content type và size bytes. Key không được chứa `..`, slash/backslash traversal và hash phải là SHA-256 hợp lệ.

`event_object_keys()` tại `storage/keys.py:111` yêu cầu station id an toàn `[A-Za-z0-9_-]{1,128}`, canonical UUID và epoch để tạo deterministic keys:

```text
<station>/<YYYY>/<MM>/<DD>/<event-uuid>/source/selected_frame.jpg
<station>/<YYYY>/<MM>/<DD>/<event-uuid>/source/label_crop.png
<station>/<YYYY>/<MM>/<DD>/<event-uuid>/metadata/job.json
<station>/<YYYY>/<MM>/<DD>/<event-uuid>/result/result.json
```

`ArtifactKeyPolicy` kiểm tra exact bucket/key/content type/size và result reference. Station upload theo thứ tự selected frame, label crop, job; sau đó mới chuyển `LOCAL_ONLY -> ARTIFACTS_READY`. Terminal record upload selected/crop/result và chuyển `TERMINAL_RESULT_DURABLE`.

`MinioArtifactStore` tại `storage/minio_store.py:37` đang dùng `minio-py==7.2.20` và private client `_put_object` với conditional `If-None-Match:*`, metadata/checksum và readback. Mục đích là idempotent same-content, không silent overwrite khi same key khác content, deterministic key và safe retry. Private API này **chưa được real MinIO runtime verified**. `DeferredArtifactStore` lazy-connect/validate trong delivery path và reset client khi lỗi; station startup không cần validate MinIO.

## 13. RabbitMQ

`TopologyConfig` tại `messaging/topology.py:9` định nghĩa:

- durable direct exchange `vision.inspection.x`;
- process route `inspection.process`;
- main queue `vision.inspection.q`;
- final DLQ `vision.inspection.dlq`;
- retry queues/keys 5 s, 30 s, 120 s, dead-letter lại process;
- prefetch mặc định 1.

`RabbitTopology.declare()` declare exchange/queues/retry/DLQ và QoS. `PikaConfirmedPublisher` bật publisher confirms, persistent delivery mode 2, mandatory publish, `message_id` và `correlation_id` là event id, content type/schema type và attempt header.

`FrozenJobPublisher` chỉ publish `INFERENCE_JOB` khi artifact state đã là `ARTIFACTS_READY`; chỉ advance state sau confirmed publish. Terminal result không được publish thành inference job.

`RetryingWorkerMessageHandler` validate message size/identity, gọi worker, yêu cầu durable result. Retry/DLQ được publish-confirm trước khi ACK message cũ. Nếu publish retry/DLQ thất bại, message cũ vẫn unacked để broker redeliver; worker process có thể dừng consumer/connection. Mô hình là at-least-once, không phải exactly-once.

Real Rabbit publisher confirms, TTL/DLX, reconnect và redelivery trên deployment thật chưa được runtime verified.

## 14. Worker lifecycle

`InferenceWorker` tại `worker/inference_worker.py:84` không có camera. Startup của `scripts/run_worker.py`:

1. validate worker config, trong đó Phase 2 yêu cầu PP-OCRv6;
2. connect/validate MinIO bucket;
3. load/warmup OCR và prepare ZXing;
4. connect Rabbit, declare topology và tạo publisher;
5. emit `WORKER_READY`;
6. consume với `auto_ack=False`.

Mỗi message:

1. parse/size-check và strict `InspectionJob` validation;
2. kiểm tra profile compatibility và `ArtifactKeyPolicy`;
3. nếu result durable đã tồn tại và provenance/identity khớp thì bỏ qua inference;
4. `get_verified(label_crop)` từ MinIO, kiểm SHA/checksum, PNG header/pixel limit và decode;
5. dựng `PreparedInspection`;
6. chạy processor: OCR inline song song ZXing background → extraction → validation;
7. tạo strict `InspectionResult` với provenance/timing;
8. `put_if_absent(result)`, readback verify;
9. trả durable disposition rồi ACK.

Retryability: contract/profile/message/image lỗi thường non-retryable; storage/readiness/unknown errors thường retryable theo policy. Worker không có reconnect loop nội bộ hoàn chỉnh; systemd restart là cơ chế recovery chính.

## 15. PP-OCRv6

`PPOCRV6TransformersAdapter` tại `ocr/ppocr_v6.py:12` load `paddleocr.PaddleOCR` một lần với `ocr_version`, `engine="transformers"`, configured device và tắt các task orientation/unwarping/textline orientation không dùng.

`warmup()` chạy input zero, đặt adapter ready. `recognize()` tái sử dụng predictor resident và normalize lines. Manual POC ép engine `ppocr_v6`; Phase 2 worker config cũng yêu cầu PP-OCRv6. Load/warmup chỉ tính startup timing, không tính vào per-inspection `ocr_ms`.

OCR nhận `PreparedInspection.label_crop`, không nhận nguyên frame. Generic `PPOCRAdapter` và `TensorRTOCRAdapter` vẫn tồn tại; TensorRT path có code nhưng là optional/experimental, không phải active runtime flow hiện tại.

## 16. ZXing và DataMatrix

`ZXingBarcodeDecoder` tại `barcode/zxing.py:10` load `zxingcpp` trong `prepare()`. `decode()` có thể thử original, grayscale và equalized variants; normalize position và dedup theo `(value, format)`. Processor chọn selected code theo tuple ưu tiên value/valid/confidence nhưng vẫn giữ danh sách decoded items.

ZXing độc lập với OCR và chạy trên cùng label crop. Trong processor hiện tại, ZXing được submit vào một background worker còn OCR chạy inline trên caller thread đã load/warmup model; hai stage vẫn overlap và extraction/validation chỉ chạy sau khi cả hai nhánh hoàn tất. Executor được tạo theo từng inspection, còn OCR model và ZXing decoder vẫn resident trong cùng `InspectionProcessor` và không bị load lại.

`BarcodeResult(value=None)` mặc định success/state có thể là SUCCESS; với `barcode_required=false`, không có code không nhất thiết làm business status fail nếu không có lý do khác. Import/runtime failure thì là error. DataMatrix exact payload/format/validity được giữ ở result contract/evaluation layer; runtime acceptance thật vẫn phụ thuộc camera/label quality.

## 17. FieldExtractor

`FieldExtractor` tại `extraction/fields.py:31` khởi tạo toàn bộ configured fields là `NOT_FOUND`, quét OCR lines và adjacent line pairs nếu enabled, chọn match có confidence tốt nhất. Không gọi LLM/VLM.

Profile DGX tại `extraction/profiles.py:11` gồm:

```text
customer_part_number
so_number
our_part_number
quantity
net_weight
gross_weight
carton_number
```

Regex hiện nhận các label như `S/O NO`, `OUR PART NO`, `Q'TY`, `N.W`, `G.W`, `C/NO`; adjacent lines cho phép giá trị nằm ở dòng kế tiếp. `Carton ID` không nằm trong alias `carton_number` hiện tại.

`customer_part_number` hiện có alias `NVIDIA P/N`. Đây là behavior production hiện tại nhưng semantics chưa được business confirm; raw OCR vẫn cần giữ line `Nvidia P/N` để trace. Chưa có field `nvidia_part_number`, và tài liệu này không tự tạo mapping mới.

Numeric/weight runtime hiện chủ yếu giữ value dạng string; evaluation có normalization rule riêng, nhưng domain model chưa biến thành typed quantity xuyên suốt pipeline.

## 18. LabelValidator

`LabelValidator` tại `validation/rules.py:11` kiểm tra quality, OCR outcome, required field presence/confidence, barcode và optional hard-fail patterns.

- Quality fail → `REVIEW` với quality reasons.
- Quality technical error → `ERROR`.
- OCR failure → `ERROR`.
- Missing hoặc low-confidence required fields → `REVIEW`.
- Invalid barcode → `FAIL`; barcode required nhưng không có code → `REVIEW`.
- Hard failure → `FAIL`; review reasons → `REVIEW`; không có issue → `PASS`.
- Phase 2 technical `ERROR` giữ business status null; business `PASS/REVIEW/FAIL` chỉ dùng cho completed result.

Một hạn chế wiring hiện tại: profile có `field_patterns`, nhưng `app.build_processor()` chỉ truyền required fields, barcode_required và min confidence vào validator; regex format profile chưa được truyền để post-validate đầy đủ DGX field format.

Required fields mặc định trong Settings là `sku`; manual DGX flow override về profile tuple. Đây là khác biệt cần nhớ khi chạy CLI khác nhau.

## 19. Processing State Machine

Enum tại `contracts/core.py`:

```text
CREATED -> CAPTURED -> PREPARED -> QUEUED -> PROCESSING -> COMPLETED
   |          |           |           |            |            |
   +----------+-----------+-----------+------------+------------> ERROR
```

Ownership hiện tại:

| State | Owner |
|---|---|
| `CREATED`, `CAPTURED`, `PREPARED` | station |
| `QUEUED` | confirmed publisher |
| `PROCESSING` | worker |
| `COMPLETED`, `ERROR` | station hoặc worker tùy branch |

Không phải mọi state đều đi qua trong local POC; local POC trả legacy `InspectionResult` đồng bộ. Phase 2 job phải publish từ `PREPARED`, không nhận job arbitrary state.

## 20. Business State Machine

Business status chỉ có sau khi đã có đủ observation để ra quyết định:

```text
preparation/quality
        |
        +-- technical failure --------------------> ERROR (business null)
        |
        +-- quality rejected ---------------------> COMPLETED + REVIEW
        |
        +-- inference ----------------------------> validate
                                                     |
                                      +--------------+--------------+
                                      |              |              |
                                      v              v              v
                                    PASS           REVIEW          FAIL
```

`PASS` không có nghĩa là OCR tuyệt đối đúng nếu required schema/profile không đầy đủ hoặc business mapping chưa được confirm. Đặc biệt alias `Nvidia P/N -> customer_part_number` là semantic blocker, phải được thể hiện trong provenance/evaluation interpretation.

## 21. Delivery và persistence states

```text
LOCAL_ONLY
    |
    | upload selected/crop/job thành công, kiểm checksum/readback
    v
ARTIFACTS_READY
    |
    | Rabbit confirmed publish exact frozen job bytes
    v
JOB_PUBLISHED
```

Với terminal branch không cần inference:

```text
LOCAL_ONLY -> ARTIFACTS_READY -> TERMINAL_RESULT_DURABLE
```

`DeliveryStatus` owner là `spool_dispatcher`. Khi MinIO/Rabbit unavailable, local commit vẫn là source of durability; delivery state vẫn `LOCAL_ONLY`, pump retry later và health phải phản ánh degraded delivery, không biến thành camera failure. Đây là contract Phase 2 hiện tại, nhưng full real infrastructure verification còn pending.

## 22. Failure branches matrix

| Failure | Layer | Local behavior | Phase 2 behavior |
|---|---|---|---|
| Camera open failed | camera | `OPEN_FAILED`, retry/open loop rồi error nếu không có frame | station không capture-capable |
| Camera read failed | camera | release/reconnect, health stale | acquisition health degraded; no fresh trigger |
| No fresh packet | trigger/buffer | `NO_FRESH_FRAME_AT_TRIGGER` | không commit inference job; technical error record nếu flow yêu cầu |
| Empty snapshot | selector | `NO_FRAME_CANDIDATE` | preparation không tạo job |
| Detector exception | detector | `DETECTION_RUNTIME_ERROR` | local terminal technical error |
| No detection | detector | `LABEL_NOT_DETECTED`/review-compatible legacy result | terminal `ERROR` hoặc review theo preparation contract |
| Crop exception | crop | `CROP_FAILED` | terminal technical error |
| Quality rejected | quality | no OCR/barcode, `REVIEW` | terminal `COMPLETED/REVIEW`, no inference job |
| OCR import/load fail | OCR | OCR failed/error | worker not ready hoặc retryable readiness error |
| OCR runtime fail | OCR | stage failed, technical/error result | worker disposition theo retry policy |
| ZXing unavailable | barcode | barcode stage error/validation review | worker error policy |
| Required field missing | validator | `REVIEW` | completed result with review |
| Barcode invalid | validator | `FAIL` | completed result with fail |
| Spool capacity exceeded | durability | reject/backpressure before commit | no data loss claim; caller must retry/alert |
| Atomic commit failure | durability | no final record should be considered committed | retry/error, inspect temp/recovery |
| MinIO unavailable | delivery | local POC does not use MinIO | station remains local-only; dispatcher retries |
| Rabbit unavailable | delivery | local POC does not use Rabbit | local-only/artifacts-ready state cannot advance to publish |
| Existing same-key same-content | storage | idempotent success expected | safe retry/readback |
| Existing same-key different-content | storage | conflict, must not overwrite silently | quarantine/error path |
| Retry/DLQ publish fail | messaging | n/a | original message remains unacked |
| Existing durable result | worker | n/a | verify identity/provenance and skip duplicate inference |

## 23. Artifact layout

### Local manual POC

`artifacts/manual_rtsp_inspection/<event_id>/` hoặc `artifacts/manual_rtsp_yolo/<event_id>/` thường chứa:

```text
selected_frame.jpg
label_crop.jpg
result.json
```

Miss/no-fresh-frame có thể chỉ có `result.json` và detector debug/input tùy branch/version. Artifact path được trả trong result JSON để operator copy về workstation.

`label_crop.jpg` được encode trực tiếp từ snapshot của đúng `PreparedInspection.label_crop` đã đưa vào OCR/ZXing; manual runtime không crop lại từ bbox. Vì định dạng JPEG là lossy, “exact” ở đây chỉ source crop/geometry và thời điểm snapshot, không phải decoded pixel equality với ndarray gốc.

### Phase 2 local spool

Xem section 10: `selected_frame.jpg`, `label_crop.png`, `job.json`/`result.json`, `state.json` dưới canonical event UUID.

### Phase 2 MinIO

Xem section 12: deterministic source/metadata/result key theo station/date/event. Worker chỉ download `label_crop`, không cần download selected frame để inference.

Không có retention/TTL/deletion policy hoàn chỉnh trong source hiện tại; disk/object growth cần operational policy riêng.

## 24. Configuration

Nguồn config chính là `src/label_inspection/config.py`; `Settings.load_dotenv()` đọc dotenv, environment có thể override defaults và CLI scripts có override riêng. `.env.example` là template, không phải runtime secret.

| Nhóm | Các tham số hiện tại |
|---|---|
| Identity | `VISION_STATION_ID`, `VISION_CAMERA_ID` |
| Camera | `VISION_RTSP_URL`, open/read timeout, reconnect/backoff |
| Buffer | size 8, window 800 ms, max frame age 1000 ms |
| Orientation | `VISION_CAMERA_ROTATE_DEGREES`, 0/90/180/270 |
| ROI/crop | normalized label ROI, padding `.05`, optional rectify |
| Detector | fixed-roi/contour/yolo, model path, device, conf `.25`, IoU `.45`, imgsz 640, max_det 10 |
| OCR | engine/backend/version/device; Phase 2 yêu cầu PP-OCRv6 |
| Validation | OCR confidence `.70`, required fields, `barcode_required=false` mặc định |
| Quality | dimensions, sharpness, brightness, exposure, glare thresholds |
| Candidate score | detection/sharpness/exposure/area/freshness/glare/validity weights |
| Spool | root, max pending events/bytes, min free disk |
| MinIO | endpoint, access/secret, bucket, TLS/timeout |
| Rabbit | AMQP URL, exchange/queue/retry topology, prefetch |
| Worker limits | max job bytes, max crop bytes/pixels, retry delays |

Phase 2 infra env nằm ở `infra/phase2/.env` khi operator tạo từ template; credentials không được commit. Compose hiện publish service vào loopback host. CLI manual có thể ép `--detector yolo`, `--detector-model`, `--detector-device`, `--device`, `--roi`, `--rotate-deg`, `--triggers`, `--debug-dir`.

Runtime assumption quan trọng: `python`/virtualenv, PaddleOCR/transformers, OpenCV, zxingcpp, torch/Ultralytics nếu dùng YOLO, Docker Compose nếu dùng Phase 2. `gpu:0` phải được mapping thành CUDA device thật; CPU fallback không được ngầm coi là GPU success.

## 25. Provenance và reproducibility

Phase 2 contract/result và runtime reports có thể ghi:

- `run_id`, generated time;
- git commit/dirty state nếu repository metadata truy cập được;
- dataset manifest/fingerprint cho evaluation;
- config fingerprint;
- detector/ROI/orientation/quality thresholds;
- OCR engine/backend/model/profile/extractor mapping version;
- dependency versions;
- stage timings và runtime device metadata.

Station provenance giữ `locator_version` để tương thích contract hiện tại và thêm structured `producer.locator`. FixedROI ghi normalized ROI; YOLO ghi model SHA-256, configured/actual device, confidence, IoU, image size, max detections và class mapping. Absolute model path không được đưa vào frozen provenance.

Local manual result có event/selected frame/timings/artifact paths nhưng provenance không đầy đủ như Phase 2 worker result. POC event ID `INS-...` khác canonical UUID Phase 2. Vì vậy cần ghi rõ path và version khi so sánh result giữa hai flow.

Provenance không thay thế ground truth. Nó cho biết result được tạo bằng semantics/config nào, đặc biệt cần dùng để trace alias `NVIDIA P/N` và detector checkpoint.

## 26. Time và timestamps

- Camera packet dùng `time.time()` cho `captured_at` và `time.monotonic()` cho freshness/duration.
- `TriggerEvent`/Phase 2 contracts persist Unix epoch milliseconds.
- `source_timestamp_ms` là optional vì RTSP/OpenCV hiện không cung cấp camera clock đáng tin cậy.
- Freshness không được tính từ timestamp do phone gửi nếu timestamp đó chưa được trust; hiện dựa trên lúc GX10 nhận frame.
- `manual_rtsp_inspection.py` dùng event id local và timing monotonic; `InspectionPipeline` có internal `trigger_id` riêng.
- Không có clock synchronization service, distributed trace clock correction hoặc camera hardware trigger timestamp trong current implementation.

## 27. Idempotency, retry và recovery

### Local durability

Commit event sử dụng temp directory + fsync + atomic rename. `open_record()` xác minh manifest. State advance chỉ đi một bước monotonic; lặp lại cùng target là idempotent.

### Artifact delivery

Object key deterministic theo station/date/event. Same-content same-key được kỳ vọng idempotent; different content cùng key phải conflict, không overwrite im lặng. Store readback verify SHA/size/content.

### Messaging

Publisher confirm trước state transition. Worker ACK sau khi result durable. Retry queues có TTL/DLX. Đây là at-least-once; duplicate message có thể xảy ra, được giảm bằng existing result + provenance identity check.

### Recovery

`scan_recovery()` tìm record pending/corrupt/incomplete không destructive. Dispatcher retry delivery. Worker systemd restart khi process fail. Không có station supervisor, centralized recovery scheduler hay automatic retention cleanup.

## 28. Security và trust boundaries

Trust boundaries chính:

1. Phone/network camera: frame là untrusted input.
2. Station process: validate size/ROI/config trước tạo job.
3. Local filesystem/spool: path containment, symlink rejection, hash/size manifest.
4. MinIO: artifact persistence; key policy và checksum/readback bảo vệ integrity.
5. RabbitMQ: transport; message size/identity/schema validation trước worker.
6. OCR/barcode libraries: native/third-party parsers xử lý image bytes giới hạn.

Các control hiện có gồm safe station/key IDs, canonical UUID contract, path traversal checks, no symlink record files, max job/crop bytes/pixels, strict schema unknown-field rejection, credentials qua env và MinIO app user policy không cấp delete theo bootstrap.

Giới hạn: Compose bind loopback phù hợp single-host; TLS/secret rotation/central IAM chưa hoàn chỉnh; camera URL và broker endpoint vẫn là operational trust inputs; no authn/authz API layer vì chưa có service API.

## 29. Logging và observability

`StructuredLifecycleLogger` tại `messaging/observability.py:21` emit one-line JSON gồm event/component/stage/status, safe timing/error fields; sensitive field names bị reject/mask.

Local POC stdout gồm camera health, frame counts, event, OCR/barcode/fields/validation, artifact paths và per-stage timings. Summary có OCR, barcode, parallel-inference và total-inspection p50/p95 trên mọi inference attempt; PASS-only E2E latency được giữ riêng. Evaluation tạo report local. Worker/station có lifecycle messages nhưng không có centralized sink.

Chưa có:

- OpenTelemetry trace propagation;
- Prometheus exporter/metrics backend;
- Grafana dashboard/alert rules;
- durable event log/search;
- SLO/alerting cho stale camera, spool pressure, delivery lag, retry/DLQ.

Vì vậy hiện có timing và structured operational evidence, nhưng chưa thể claim trace ID distributed end-to-end hoặc production monitoring. Event ID/correlation ID là identity/idempotency key, không phải tracing system.

## 30. Manual RTSP và runtime tools

| Tool | Vai trò |
|---|---|
| `scripts/manual_rtsp_inspection.py` | interactive Enter/timed manual trigger, POC JSON/artifacts |
| `scripts/inspect_rtsp.py` | inspect RTSP stream path |
| `scripts/run_real_rtsp_integration.py` | real stream integration helper |
| `scripts/inspect_image.py` | single image pipeline |
| `scripts/run_real_image_integration.py` | real image integration helper |
| `scripts/run_station.py` | Phase 2 station process |
| `scripts/run_worker.py` | Phase 2 worker consumer |
| `scripts/check_runtime.py` | dependency/device/runtime checks |
| `scripts/debug_yolo_detector.py` | detector-only debug |
| `scripts/test_ppocr_v6.py` | OCR runtime check |
| `scripts/test_zxing_runtime.py` | ZXing runtime check |
| `scripts/benchmark_selector.py` | selector benchmark |
| `scripts/evaluate_dataset.py` | dataset evaluator, not live station path |

Manual RTSP POC là continuous capture + bounded memory buffer, không tự động capture từng inspection nếu chưa trigger. `--debug-dir` lưu evidence theo event; artifact path in result JSON phải được copy/inspect trên đúng host hoặc scp về workstation.

## 31. Test strategy

### Unit/contract tests

Các test hiện có bao phủ config wiring, camera/frame buffer/selector, crop/quality/orientation, detectors, OCR/barcode adapters, fields/validator, contracts, spool, storage policy, Rabbit topology/publisher/retry, worker và Phase 2 boundaries.

### Local integration

Một số test dùng fake/in-memory transports, `InMemoryArtifactStore`, temporary filesystem và synthetic/local image. Chúng chứng minh logic/state/contract, **không** chứng minh MinIO/Rabbit thật, driver ARM64, broker confirms thật hoặc native camera runtime.

### Runtime smoke/evidence

Có scripts kiểm CUDA/model load, OCR/ZXing và POC phone RTSP. Một artifact lịch sử cho thấy YOLO + PP-OCRv6 + ZXing từng trả `PASS` trên một frame; đó là sample evidence, không phải acceptance thống kê.

### Missing acceptance coverage

- full station + worker + real MinIO + real Rabbit trên GX10;
- actual MinIO conditional PUT/private API/readback race behavior;
- Rabbit confirm/TTL/DLX/reconnect/redelivery thật;
- Linux fsync/symlink/disk-full/recovery matrix;
- 50-attempt YOLO live acceptance và OCR/field accuracy theo ground truth;
- centralized metrics/traces/alerts.

## 32. Current known issues và technical debt

1. Full production E2E on real infrastructure chưa được chứng minh; local fakes không được nâng cấp thành runtime claim.
2. YOLO detector vẫn `EXPERIMENTAL`; recall, camera pose, threshold và 50-attempt acceptance chưa chốt.
3. Local POC event id `INS-...` không đồng nhất canonical UUID Phase 2.
4. OCR và ZXing đã có parallel stage path; OCR được giữ trên caller/warmup thread còn ZXing chạy background. Actual native-library speedup vẫn cần benchmark GX10, vì Python-level concurrency không tự chứng minh hai thư viện luôn release GIL/không serialize nội bộ.
5. Profile regex chưa được wire vào validator qua `build_processor()`.
6. Weight/numeric values còn string ở runtime domain; normalization chủ yếu thuộc evaluation.
7. `NVIDIA P/N -> customer_part_number` là known semantic blocker cần business confirmation.
8. `MinioArtifactStore` dùng private `_put_object`; real runtime chưa verify.
9. Quality thresholds và candidate scoring là provisional/heuristic, chưa camera-calibrated.
10. Chỉ worker có systemd unit; station chưa có supervisor.
11. Không có metrics backend, trace system, dashboard/alerting.
12. Không có industrial camera adapter, hardware trigger hoặc trusted source timestamp.
13. Retention/cleanup cho local spool và MinIO object chưa được hoàn thiện.
14. Tài liệu lịch sử có drift: một số file từng mô tả YOLO deferred hoặc MinIO probe đã bỏ, nhưng source hiện đã có YOLO integration và deferred delivery. Khi mâu thuẫn, source hiện tại là authority.

## 33. Production readiness matrix

| Area | Current assessment | Exit evidence cần có |
|---|---|---|
| Camera acquisition | POC ready | sustained run, reconnect, stale/recovery trên target camera |
| Frame selection | Implemented heuristic | calibrated Top-K/quality correlation với OCR success |
| FixedROI | usable for fixed setup | per-camera ROI calibration + operator verification |
| YOLO localization | Experimental | live recall/latency/false-detection acceptance |
| OCR | resident implementation | target-label accuracy/latency over verified dataset |
| Barcode | real adapter implementation | exact payload/format/valid/no-code/multiple-code matrix |
| Field extraction | regex profile | business-confirmed semantics + field-level accuracy |
| Quality gate | provisional | lighting/glare/blur/distance/rotation calibration |
| Local spool | local durability implementation | GX10 filesystem fault/recovery tests |
| MinIO delivery | code + local tests | real MinIO version/API/race/readback test |
| Rabbit delivery | code + local tests | real confirm/TTL/DLX/reconnect/redelivery test |
| Worker | process implemented | two-process sustained E2E with recovery |
| Security | local controls | secret/TLS/IAM/host hardening review |
| Observability | logs/timings only | trace/metrics/alerts/SLO operational proof |
| Deployment | Compose infra + native apps | repeatable GX10 service/runbook acceptance |
| Overall | NOT production accepted | all critical runtime/business evidence above |

## 34. Current end-to-end flows A–F

### Flow A — Single image local

```text
image path -> build_pipeline -> StationPreparer -> detector/crop/quality
 -> InspectionProcessor ((OCR || ZXing) -> fields -> validator)
 -> local InspectionResult + optional artifacts
```

### Flow B — Manual phone RTSP local

```text
phone RTSP/HTTP -> RTSPCamera -> acquisition -> ring buffer
 -> Enter -> fresh snapshot -> Top-K -> orientation
 -> FixedROI or YOLO -> crop/quality -> (OCR || ZXing)
 -> extractor/validator -> stdout JSON + debug artifacts
```

### Flow C — Phase 2 station quality terminal

```text
camera -> trigger -> preparation -> quality FAIL
 -> terminal result in local spool -> optional upload
 -> LOCAL_ONLY -> ARTIFACTS_READY -> TERMINAL_RESULT_DURABLE
```

### Flow D — Phase 2 station inference job

```text
camera -> trigger -> prepare PASS -> atomic spool job
 -> upload selected/crop/job -> ARTIFACTS_READY
 -> confirmed Rabbit publish -> JOB_PUBLISHED
```

### Flow E — Phase 2 worker inference

```text
Rabbit job -> strict parse/policy -> MinIO verified label_crop
 -> (PP-OCRv6 || ZXing) -> extraction -> validator
 -> put/readback result -> terminal durable result
```

### Flow F — Delivery unavailable/recovery

```text
station starts + camera fresh + local spool works
 -> MinIO/Rabbit unavailable -> LOCAL_ONLY / DEGRADED delivery
 -> background retry
 -> MinIO available -> upload -> ARTIFACTS_READY
 -> Rabbit confirm -> JOB_PUBLISHED -> worker result durable
```

Flow F is the intended Phase 2 contract. Its real GX10 infrastructure proof remains pending in this repository snapshot.

## 35. Current versus future

| Concern | Current | Future/extension, not implemented |
|---|---|---|
| Camera | phone RTSP/HTTP via OpenCV | industrial USB/GigE SDK/hardware trigger |
| Localization | FixedROI stable path; YOLO experimental | production-trained detector acceptance |
| Inference | PP-OCRv6 + ZXing | TensorRT optimization if separately verified |
| Transport | RabbitMQ | no Kafka/Redis requirement in current flow |
| Artifact store | MinIO | HA/object lifecycle policy |
| Database | local spool + object metadata | PostgreSQL/event query store |
| Observability | JSON logs/timings | OTel/Prometheus/Grafana/alerting |
| Deployment | native station/worker + Docker infra | orchestrated multi-node deployment |
| Trigger | manual/timed | PLC/hardware trigger and debounce |
| Business mapping | current alias, pending confirmation | separate approved schema change |

Các future item này không phải dependency hiện tại và không được coi là đã triển khai chỉ vì có optional package, class hoặc roadmap text.

## 36. Extension points

- `CameraSource` protocol cho camera adapter khác; cần giữ frame/timestamp/health semantics.
- `LabelDetector` protocol cho FixedROI/YOLO hoặc detector mới.
- `OCRAdapter`/barcode interfaces cho engine khác nhưng phải giữ resident lifecycle và result schema.
- `ArtifactStore` cho storage backend khác, phải giữ idempotency, containment, checksum/readback.
- `Publisher`/topology abstraction cho transport khác, phải giữ confirmed publish trước state transition.
- Extraction profiles cho từng label/business schema; profile version và semantic mapping phải nằm trong provenance.
- `InspectionJob`/`InspectionResult` contract versioning cho service boundary.
- `StructuredLifecycleLogger` là hook cho log sink tương lai, nhưng không tự cung cấp tracing.

Mọi extension phải giữ invariants ở section 39 và không được dùng “optional dependency tồn tại” làm bằng chứng runtime support.

## 37. File và module map

| Khu vực | File/module | Trách nhiệm |
|---|---|---|
| App wiring | `src/label_inspection/app.py` | validate config, build detector/preparer/processor |
| Config | `src/label_inspection/config.py` | Settings, dotenv/env, validation |
| Local schema | `src/label_inspection/schemas.py` | POC result/stage models |
| Contracts | `src/label_inspection/contracts/core.py` | IDs/statuses/ArtifactRef/TriggerEvent |
| Job/result contracts | `contracts/job.py`, `contracts/result.py` | strict Phase 2 JSON contracts |
| Camera | `camera/base.py`, `rtsp.py`, `acquisition.py`, `frame_buffer.py`, `selector.py` | capture/buffer/freshness/Top-K |
| Detection | `detection/base.py`, `fixed_roi.py`, `contour.py`, `ultralytics_detector.py` | localization implementations |
| Preprocess | `preprocessing/orientation.py`, `crop.py`, `rectify.py`, `quality.py` | transform/crop/quality |
| Pipeline | `pipeline/inspection.py`, `ranking.py`, `types.py` | local façade/candidate ranking/prepared type |
| OCR | `ocr/ppocr_v6.py`, `ppocr.py`, `tensorrt_ocr.py` | OCR adapters/lifecycle |
| Barcode | `barcode/zxing.py` | ZXing decode/normalization |
| Extraction | `extraction/fields.py`, `profiles.py` | regex/profile field mapping |
| Validation | `validation/rules.py` | quality/field/barcode business decision |
| Station | `station/controller.py`, `preparation.py`, `spool.py`, `service.py`, `dispatcher.py` | capture/preparation/spool/delivery |
| Storage | `storage/base.py`, `minio_store.py`, `deferred.py`, `keys.py` | artifact store/key policy |
| Messaging | `messaging/topology.py`, `publisher.py`, `retry.py`, `observability.py` | Rabbit topology/publish/retry/log |
| Worker | `worker/inference_worker.py`, `processor.py`, `provenance.py` | inference and result durability |
| Evaluation | `evaluation/dataset.py`, `evaluator.py`, `metrics.py`, `reporting.py` | dataset validation/evaluation |
| Entrypoints | `scripts/manual_rtsp_inspection.py`, `run_station.py`, `run_worker.py` | POC/Phase 2 processes |
| Deployment | `infra/phase2/docker-compose.yml`, `ops/systemd/vision-inference-worker.service` | MinIO/Rabbit/native worker |

## 38. Dependency map

```text
Python 3.10 <= version < 3.13
  |
  +-- numpy + OpenCV ---------------- camera, crop, quality, image decode
  +-- PaddleOCR/PaddleX ------------- PP-OCRv6 DET+REC
  +-- transformers ------------------ configured OCR backend dependency
  +-- torch + ultralytics ------------ optional YOLO detector
  +-- zxing-cpp ---------------------- barcode/DataMatrix
  +-- minio -------------------------- Phase 2 artifact store
  +-- pika --------------------------- Phase 2 Rabbit publisher/consumer
  +-- python-dotenv ------------------ env/config loading
  +-- pytest ------------------------- tests
```

`pyproject.toml` có optional extras cho detector, OCR, OCR transformers, TensorRT, barcode, transport và Phase 2; `uv.lock` pin dependency graph hiện tại. Redis optional/deferred không thuộc current active runtime. Không có PostgreSQL, Kafka, Kubernetes, MediaMTX, YOLO training dependency trong inference runtime path.

Deployment dependency:

```text
GX10 host + Python venv + GPU driver/CUDA
  + Docker Engine/Compose
      + MinIO image + persistent volume
      + RabbitMQ image + persistent volume
```

Container services không thay thế Python app processes. Native process vẫn cần đúng venv/model/cache và quyền filesystem.

## 39. Ví dụ dữ liệu end-to-end

Ví dụ dưới đây là shape rút gọn để mô tả contract, không phải fixture acceptance:

```text
TriggerEvent
  event_id: <canonical UUIDv4 trong Phase 2>
  trigger_id: <canonical UUIDv4>
  station_id: STATION-01
  camera_id: PHONE-01
  triggered_at_ms: <epoch-ms>
        |
        v
PreparedInspection
  selected_frame_id: 678
  label_bbox: [x1, y1, x2, y2]
  label_crop: label_crop.png
  quality: PASS
  processing_status: PREPARED
        |
        v
InspectionJob
  schema: inspection-job.v1
  artifacts:
    label_crop: {bucket, deterministic-key, sha256, size_bytes}
  provenance:
    producer:
      locator_version: fixed-roi.v1 or ultralytics-yolo.v1
      locator: {type, support_level, model_sha256/roi, device, thresholds, class_mapping}
    ocr: {engine: ppocr_v6, backend: transformers, profile}
    extractor_profile: dgx_spark_label
    semantic_mapping: KNOWN_SEMANTIC_BLOCKER
        |
        v
Worker processing
  label_crop -> OCR lines + confidence
              -> ZXing items (DataMatrix/other formats)
              -> extracted fields
              -> LabelValidator
        |
        v
InspectionResult
  schema: inspection-result.v1
  processing_status: COMPLETED
  business_status: PASS | REVIEW | FAIL
  inference_executed: true
  result_payload:
    raw_ocr: <all OCR lines/evidence>
    fields: <profile fields; missing may be null>
    barcode: <selected + all detected items>
    quality: <observations>
  error: null
```

Local POC có thể dùng `event_id=INS-35D1B792664A` và local `InspectionResult` shape; đó là compatibility path, không phải Phase 2 canonical example. Raw OCR và barcode evidence phải được giữ để phân biệt detector success, OCR success, extraction mapping và validator decision.

## 40. Glossary

| Term | Nghĩa trong hệ thống |
|---|---|
| `CameraSource` | Interface đọc frame/health từ camera |
| `FramePacket` | Frame kèm id, capture time, monotonic time và source |
| `FrameBuffer` | Ring buffer bounded giữ frame gần nhất trong memory |
| Fresh frame | Frame nằm trong age/window cho phép tại trigger |
| Top-K | Tập tối đa K frame candidate được xếp hạng trước preparation |
| FixedROI | ROI normalized cấu hình trước cho label |
| YOLO | Detector định vị label; không phải OCR |
| Label crop | Ảnh vùng label đưa vào OCR/ZXing |
| Quality gate | Kiểm tra sharpness/exposure/size/glare trước inference |
| PP-OCRv6 | OCR DET+REC resident runtime hiện tại |
| ZXing | Barcode/DataMatrix decoder độc lập OCR |
| FieldExtractor | Regex/profile mapper từ OCR lines sang business fields |
| LabelValidator | Chuyển observations thành PASS/REVIEW/FAIL/ERROR |
| InspectionJob | Frozen Phase 2 message station gửi worker |
| InspectionResult | Frozen Phase 2 result worker/station lưu durable |
| Local Spool | Filesystem durable boundary trước delivery |
| ArtifactRef | Contract reference tới object có key/hash/size/content type |
| `LOCAL_ONLY` | Record chỉ chắc chắn ở local spool |
| `ARTIFACTS_READY` | Artifact upload đã verify, job chưa confirmed publish |
| `JOB_PUBLISHED` | Rabbit đã confirm inference job |
| `TERMINAL_RESULT_DURABLE` | Terminal result đã được upload/readback durable |
| `KNOWN_SEMANTIC_BLOCKER` | Mapping business tồn tại trong code nhưng chưa được xác nhận |
| `RUNTIME PENDING` | Có code/test local hoặc evidence giới hạn, chưa có real deployment proof |
| At-least-once | Message có thể redeliver; ACK sau durability |
| Provenance | Metadata để tái hiện config/code/model/semantic mapping của run |
| POC | Proof-of-concept path, không tự động là production-ready |
