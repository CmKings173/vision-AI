# Vision AI V2 — Codebase Context

> Source of truth cho các task tiếp theo. Tài liệu này mô tả code đang có trong
> repository tại thời điểm `2026-08-22`, không mô tả toàn bộ roadmap trong
> `tasks/plan.md`.

## 1. Executive summary

Đây là một Python package độc lập cho **OCR/label inspection V1**. Pipeline
hiện tại chạy trong một process, nhận image hoặc một tập `FramePacket`, chọn
frame/candidate tốt nhất, crop/quality-check, chạy đúng một lần OCR và barcode,
extract SKU/LOT, rồi trả về JSON có status `PASS`, `FAIL`, `REVIEW` hoặc `ERROR`.

Runtime active hiện chỉ có các CLI script; chưa có HTTP server, worker, Redis
transport, GLM-OCR sidecar hay camera USB/GigE adapter.

Các quyết định vận hành quan trọng:

- `FixedROI` là detector duy nhất được coi là `SUPPORTED` cho GX10 V1.
- `ContourDetector` tồn tại nhưng `EXPERIMENTAL`.
- `UltralyticsLabelDetector` tồn tại nhưng `DEFERRED`; quyết định license là
  blocker trước commercial deployment.
- `PPOCRAdapter` là OCR local mặc định; `TensorRTOCRAdapter` là backend native
  tùy chọn cần engine được build trên đúng GPU target.
- `Top-K` là ngân sách chọn frame/candidate, **không phải K lần OCR**. Pipeline
  hiện chỉ gửi candidate tốt nhất vào OCR và barcode.
- GLM-OCR, Redis Streams, custom detector training và deployment hardening là
  roadmap, chưa phải implementation.

## 2. Repository shape và phạm vi

```text
.
├── README.md
├── pyproject.toml
├── .env.example
├── scripts/
│   ├── inspect_image.py          # image -> inspection JSON
│   ├── replay_video.py           # video -> bounded buffer/top-K; optional pipeline
│   ├── inspect_rtsp.py            # RTSP -> bounded buffer -> inspection JSON
│   ├── camera_smoke.py            # RTSP capture smoke/health
│   ├── benchmark_selector.py     # preview selector benchmark
│   ├── check_runtime.py           # import/GPU/engine readiness
│   └── build_tensorrt_engine.py   # ONNX -> target-specific TensorRT engine
├── src/label_inspection/
│   ├── app.py                     # composition root: Settings -> pipeline
│   ├── config.py                  # dotenv/environment-backed Settings
│   ├── schemas.py                 # domain objects + JSON contract
│   ├── runtime.py                 # readiness checks
│   ├── timing.py                  # canonical stage timing keys
│   ├── smoke.py                   # CLI exit-code semantics
│   ├── camera/
│   ├── detection/
│   ├── preprocessing/
│   ├── ocr/
│   ├── barcode/
│   ├── extraction/
│   ├── validation/
│   └── pipeline/
├── tests/                         # 19 test files, 79 test functions
└── tasks/
    ├── plan.md                    # original implementation plan/intent
    ├── todo.md                    # historical task status
    └── codebase_context.md        # this document
```

`models/`, `.env`, virtualenv, build output và cache bị ignore bởi `.gitignore`.
Repository không chứa ONNX/engine/character-dictionary/model artifact nào.

Codebase này được tạo như một project mới, isolated với legacy path được nhắc
trong README/plan. Không có import nào tới legacy project và không có runtime
dependency tới Gemini, DeepFace, Qwen-VL, vLLM hoặc robot-agent code.

## 3. Entrypoints và end-to-end flows

### 3.1 Entrypoint thực tế

Không có `label_inspection.__main__`, console script, HTTP app hoặc service
runner. Các entrypoint thực tế đều là `scripts/*.py`; mỗi script tự thêm
`src/` vào `sys.path`.

| Command | Vai trò | Output/exit |
|---|---|---|
| `python scripts/inspect_image.py --image ...` | Đọc một image bằng OpenCV và chạy pipeline | inspection JSON; `0` cho completed kể cả `REVIEW/FAIL`, `1` cho pipeline failure, `2` cho local/runtime/config error |
| `python scripts/replay_video.py --source ...` | Đọc tối đa N frame, giữ buffer bounded, báo top-K | JSON selection; `--pipeline` chạy thêm một inspection |
| `python scripts/inspect_rtsp.py [--source ...]` | Capture bounded RTSP window trên acquisition daemon rồi inspect | inspection JSON hoặc structured capture error |
| `python scripts/camera_smoke.py [--source ...]` | Chỉ kiểm tra RTSP read/health/frame metadata | `0` nếu nhận được frame, `1` nếu không |
| `python scripts/check_runtime.py` | Import thật OpenCV/NumPy/ZXing/Paddle hoặc TensorRT; kiểm tra device/path | `1` nếu có check `FAIL` |
| `python scripts/test_zxing_runtime.py --image ...` | Runtime ZXing-C++ thật trên một image; position JSON primitive | `0` nếu đạt số barcode tối thiểu |
| `python scripts/test_ppocr_v6.py --image ...` | Runtime PP-OCRv6 Transformers thật; warmup và benchmark | `0` nếu OCR thật trả text |
| `python scripts/run_real_image_integration.py --image ...` | FixedROI image integration thật và benchmark 20 run | dừng trước benchmark nếu image không `PASS` |
| `python scripts/run_real_rtsp_integration.py ...` | RTSP thật -> bounded buffer -> FixedROI -> OCR/ZXing | chỉ chạy sau image integration `PASS` |
| `python scripts/benchmark_selector.py ...` | So sánh full-frame score cũ với preview score | JSON benchmark, không phải production path |
| `python scripts/build_tensorrt_engine.py ...` | Tool build artifact | ghi `.engine` ra path chỉ định; cần TensorRT/ONNX/GPU target |

`manual_rtsp_inspection.py` is the direct phone-camera POC entrypoint: it uses
the same `RTSPCamera`/`CameraSource` boundary for an RTSP or HTTP URL, keeps
`CameraAcquisition` feeding the bounded `FrameBuffer`, and waits for a manual
Enter trigger (or `--trigger-after-s`) before calling the pipeline once.

### 3.2 Image flow

```text
inspect_image.py
  -> cv2.imread (BGR ndarray)
  -> replace(global Settings, CLI detector/ROI)
  -> app.build_pipeline(Settings)
  -> InspectionPipeline.inspect_frame
       -> tạo FramePacket(source="image")
       -> FrameSelector: stale filter + global preview score
       -> detector.detect(full-resolution frame)
       -> crop_image + optional rectify
       -> QualityChecker + CandidateScorer
       -> chọn một PreparedCandidate tốt nhất
       -> OCR một lần và barcode một lần trên crop đó
       -> FieldExtractor(raw_ocr.lines)
       -> LabelValidator
       -> InspectionResult.to_dict -> JSON
```

### 3.3 RTSP flow

```text
inspect_rtsp.py / camera_smoke.py
  -> resolve_camera_source(--source > VISION_RTSP_URL)
  -> mask_url_credentials cho log/error
  -> RTSPCamera (lazy import cv2, FFmpeg backend nếu có)
  -> CameraAcquisition daemon thread
       -> camera.read / open / reconnect / timeout hints
       -> append FramePacket vào FrameBuffer(deque(maxlen=N))
  -> caller chờ hard deadline, không tự gọi blocking camera I/O
  -> stop(): set event, camera.close() requests shutdown without releasing during read, bounded join
  -> snapshot stale-safe
  -> InspectionPipeline.inspect_packets
```

`RTSPCamera.read()` đóng capture khi read fail và lần read sau sẽ reconnect.
`RTSPCamera.health` theo dõi connected/stale/frame count/last error/reconnect
count. Việc `release()` có hủy được native OpenCV/FFmpeg read hay không vẫn là
assumption cần verify trên GX10; controller chỉ có bounded join.

`resolve_camera_source()` không giới hạn scheme ở `rtsp://`: URL HTTP cũng được
truyền nguyên vẹn tới cùng `RTSPCamera`/OpenCV backend. Vì vậy POC dùng app IP
Cam trên Android có thể feed trực tiếp RTSP hoặc HTTP URL vào `CameraSource`,
không có MediaMTX trong data path. `manual_rtsp_inspection.py` giữ acquisition
chạy liên tục, chờ frame đầu tiên, sau đó dùng Enter (hoặc `--trigger-after-s`)
làm manual trigger để snapshot các frame còn fresh trong ring buffer và gọi
`InspectionPipeline.inspect_packets()` đúng một lần.

`RTSPCamera` serializes native `capture.read()` and `release()`. If `close()`
arrives while a native read is active, release is deferred to a daemon cleanup
thread until that read returns; this avoids the FFmpeg demuxer assertion caused
by concurrent read/release. A backend that never returns from native read can
still leave the daemon cleanup thread alive after the controller's bounded join.

### 3.4 Video flow

`replay_video.py` dùng `cv2.VideoCapture` và
`capture_video_into_buffer()`. Frame được sample theo `sample_every`, gán
`source="video"`, append vào bounded `FrameBuffer`, rồi selector chọn top-K.
`--pipeline` truyền **các packet trong buffer** vào cùng
`InspectionPipeline.inspect_packets`; không OCR từng frame.

### 3.5 Pipeline flow và failure boundaries

`InspectionPipeline.inspect_packets()` trong
`src/label_inspection/pipeline/inspection.py` là orchestration trung tâm:

1. Materialize input iterable thành list và generate `INS-<12 hex uppercase>`
   nếu caller không truyền `event_id`.
2. Select packet fresh nhất/tốt nhất theo global preview score. Nếu không có
   packet: `ERROR` với `NO_FRAME` hoặc `STALE_FRAMES`; downstream stages
   `NOT_RUN`.
3. Với từng packet được chọn, gọi detector. Detector exception không leak ra
   ngoài; nếu không chuẩn bị được candidate nào thì `DETECTION_RUNTIME_ERROR`,
   `CROP_PREPARATION_ERROR` hoặc `LABEL_NOT_DETECTED`.
4. Với từng `LabelCandidate`: padding + clamp bbox, crop, chuyển corners sang
   crop-local coordinates, perspective rectify nếu có quadrilateral, đo quality,
   rồi tính `LabelCandidateScore`.
5. Nếu không candidate nào qua quality: chọn candidate có score cao nhất,
   trả `REVIEW` (`QUALITY_REJECTED`) hoặc `ERROR` nếu metrics runtime hỏng;
   OCR/barcode không chạy.
6. Chọn candidate tốt nhất trong nhóm quality `PASS`.
7. Gọi OCR một lần. Exception trở thành `RawOCRResult` failed với mã tổng quát,
   không đưa model path/exception detail vào JSON.
8. Gọi barcode độc lập một lần. Exception trở thành `BarcodeResult` failed.
   Decoder có thể trả nhiều kết quả; `_choose_barcode()` ưu tiên có value, valid
   và confidence.
9. Extract SKU/LOT từ raw OCR lines, không sửa raw lines.
10. Validate quality/OCR/extracted fields/barcode và ghi timing.

Current validation semantics:

- `PASS`: mọi required field có giá trị đủ confidence, format hợp lệ và không
  có hard failure; barcode không bắt buộc theo default.
- `FAIL`: field format invalid hoặc barcode có `valid=False`.
- `REVIEW`: thiếu field, confidence thấp, quality fail hoặc barcode optional bị
  lỗi/missing theo rule.
- `ERROR`: OCR runtime failure, quality metrics failure, no frame, detector/crop
  runtime failure hoặc stage không thể chạy.

## 4. Module map

### Composition, contract và shared state

| Module | Trách nhiệm | Trạng thái |
|---|---|---|
| `app.py` | Chọn detector/OCR theo `Settings`, wire selector/scorer/quality/extractor/validator/barcode | Active composition root |
| `config.py` | `Settings` frozen dataclass; load `.env` một lần khi import; `validate()` | Active; global `settings` được các script dùng |
| `schemas.py` | `FramePacket`, `LabelCandidate`, `OCRLine`, `RawOCRResult`, `BarcodeResult`, `QualityReport`, `LabelCandidateScore`, `ValidationResult`, `InspectionResult` | Active shared contract |
| `timing.py` | `TIMING_KEYS`, `new_timing()`, context manager `timed()` dùng monotonic `perf_counter` | Active |
| `smoke.py` | `SmokeExitCode` và exit mapping | Active CLI contract |
| `runtime.py` | Actual import checks, Python range, Paddle/Torch/TensorRT/CUDA readiness | Active diagnostics only |

### Camera và frame selection

| Module | Trách nhiệm | Trạng thái |
|---|---|---|
| `camera/base.py` | `CameraSource` protocol (`open/read/frames/close`) | Interface; USB/GigE chưa có implementation |
| `camera/rtsp.py` | OpenCV RTSP, FFmpeg backend, timeout properties, reconnect/backoff, health | Active RTSP path |
| `camera/acquisition.py` | Daemon reader thread, hard wait deadline, bounded shutdown | Active RTSP path |
| `camera/frame_buffer.py` | Thread-safe bounded `deque`, wall/monotonic stale filtering | Active |
| `camera/selector.py` | Preview downsample, brightness/sharpness score, freshness tie-break, top-K | Active; không chạy OCR |
| `camera/video.py` | Local `VideoCapture` -> buffer helper | Active replay path |
| `camera/security.py` | CLI/config source precedence và URL password masking | Active; không mask source trước khi connect |

`RTSPCamera.frames()`, `CameraSource.frames()`,
`FrameBuffer.wait_for_frame()`, `FrameBuffer.latest()` và
`FrameSelector.select_from_buffer()` hiện không nằm trên CLI inspection path;
chúng là API/helper được test hoặc giữ cho future integration. Không nên xóa
chúng như “dead code” nếu chưa quyết định public contract.

### Detection

| Module | Trách nhiệm | Trạng thái |
|---|---|---|
| `detection/base.py` | `LabelDetector` protocol | Active boundary |
| `detection/fixed_roi.py` | Parse/validate normalized hoặc absolute ROI; clamp; tạo một candidate | **Supported V1** |
| `detection/contour.py` | Threshold + external contours + optional quadrilateral | **Experimental**, config vẫn cho phép |
| `detection/ultralytics_detector.py` | Lazy import/load resident YOLO, normalize boxes | **Deferred**; custom shipping-label model chưa có; license blocker |

### Preprocessing và candidate ranking

| Module | Trách nhiệm | Trạng thái |
|---|---|---|
| `preprocessing/crop.py` | Padding theo ratio, clamp boundary, crop provenance/truncation | Active |
| `preprocessing/rectify.py` | Order 4 points và conditional perspective warp | Active khi detector cung cấp corners |
| `preprocessing/quality.py` | Width/height, brightness, under/overexposure, glare, Laplacian sharpness; quality gate | Active; thresholds chưa camera-calibrated |
| `pipeline/ranking.py` | Normalize weights; score detection/sharpness/exposure/area/freshness/glare/validity | Active |
| `pipeline/inspection.py` | End-to-end orchestration và structured failure | Active core |

### OCR, barcode, business logic

| Module | Trách nhiệm | Trạng thái |
|---|---|---|
| `ocr/base.py` | `OCRProvider` protocol | Active boundary |
| `ocr/ppocr.py` | Lazy PaddleOCR load-once; parse Paddle 3.x và legacy result shapes | Active default; cần PaddleOCR + PaddlePaddle target-compatible |
| `ocr/ppocr_v6.py` | Resident PP-OCRv6 `engine=transformers`; structured metadata/error boundary | Active GX10 path; cần PaddleOCR + Transformers + Torch GPU |
| `ocr/tensorrt_ocr.py` | Native TensorRT det/rec/(optional cls), CUDA buffers, PP-OCR DB/CTC postprocess | Code active; target runtime/models chưa verify |
| `barcode/base.py` | `BarcodeDecoder` protocol + `NullBarcodeDecoder` cho test/deployment no-op | Active boundary |
| `barcode/zxing.py` | Lazy ZXing-C++ import, all enabled image variants, multi-code result/position normalization, dedupe | Active default; cần `zxing-cpp` |
| `extraction/fields.py` | Regex extraction SKU/LOT plus optional Shopee tracking/order fields, giữ source line/confidence | Active; Shopee profile is opt-in through required fields |
| `validation/rules.py` | Required fields, field patterns, confidence threshold, barcode/quality rules | Active deterministic business gate |

### Script/test/documentation-only code

- `scripts/benchmark_selector.py::legacy_full_frame_score` là baseline benchmark,
  không được dùng trong production selector.
- `scripts/build_tensorrt_engine.py` là build tool, không được import bởi
  pipeline runtime.
- `tests/fixtures/quality/factory.py` tạo OpenCV image tổng hợp thật; không phải
  production fixture/model.
- `tasks/plan.md` và `tasks/todo.md` là historical intent/status. Một số path
  trong plan (`transport/`, `glm_ocr.py`, USB/GigE, worker) chưa tồn tại.

Không có module source nào được chứng minh là dead hoàn toàn qua static import
scan. Những phần deferred/dormant ở trên phải được coi là “chưa active” chứ
không tự động xóa.

## 5. Configuration và environment

`config.py` gọi `load_dotenv()` ngay khi import, tạo `settings = Settings()`.
Mỗi field dùng `os.getenv()` trong `default_factory`, nên biến môi trường được
đọc tại thời điểm tạo `Settings`, không phải khi mỗi stage chạy. `.env.example`
không tự động được đọc; cần copy thành `.env` hoặc export env.

### Camera, buffering và selection

| Env | Default trong `Settings` | Consumer/ý nghĩa |
|---|---:|---|
| `VISION_CAMERA_ID` | `PHONE-01` | ID trong `InspectionResult` |
| `VISION_RTSP_URL` | unset | fallback source cho RTSP CLI |
| `VISION_BUFFER_SIZE` | `8` | `FrameBuffer.max_size`; top-K không được vượt quá |
| `VISION_BUFFER_WINDOW_MS` | `800` | stale filter của RTSP buffer |
| `VISION_MAX_FRAME_AGE_MS` | `1000` | selector/candidate freshness/RTSP health |
| `VISION_RTSP_OPEN_TIMEOUT_MS` | `5000` | OpenCV open timeout hint |
| `VISION_RTSP_READ_TIMEOUT_MS` | `2000` | OpenCV read timeout hint |
| `VISION_TOP_K` | `3` | số frame được preselect và số candidate budget |
| `VISION_FRAME_PREVIEW_LONG_EDGE` | `480` | preview selection; bắt buộc `320..640` |

### ROI, detector và device

| Env | Default | Consumer/ý nghĩa |
|---|---:|---|
| `VISION_LABEL_ROI` | unset | bắt buộc khi detector là FixedROI; `x1,y1,x2,y2` |
| `VISION_ROI_NORMALIZED` | `true` | ROI normalized theo width/height nếu true |
| `VISION_BBOX_PADDING_RATIO` | `0.05` | padding crop, sau đó clamp |
| `VISION_DETECTOR` | `FixedROI` | `fixed-roi`, `contour`, `ultralytics/yolo` |
| `VISION_DETECTOR_MODEL` | `models/shipping_label.pt` | chỉ dùng cho Ultralytics |
| `VISION_DETECTOR_DEVICE` | `cpu` | device cho Ultralytics/runtime readiness |

### OCR và TensorRT

| Env | Default | Consumer/ý nghĩa |
|---|---:|---|
| `VISION_OCR_ENGINE` | `ppocr` | `ppocr`, `ppocr_v6`, hoặc `tensorrt` |
| `VISION_OCR_BACKEND` | `transformers` | bắt buộc là `transformers` khi dùng `ppocr_v6` |
| `VISION_OCR_VERSION` | `PP-OCRv6` | model version bắt buộc cho `ppocr_v6` |
| `VISION_OCR_DEVICE` | `cpu` | PaddleOCR device; riêng TensorRT chỉ là config metadata |
| `VISION_OCR_LANG` | `en` | PaddleOCR language |
| `VISION_OCR_CONFIDENCE` | `0.70` | ngưỡng field confidence cho validation; không loại raw OCR line |
| `VISION_OCR_DET_ENGINE` | unset | TensorRT detection engine path, bắt buộc khi TRT |
| `VISION_OCR_REC_ENGINE` | unset | TensorRT recognition engine path, bắt buộc khi TRT |
| `VISION_OCR_CLS_ENGINE` | unset | TensorRT angle classifier tùy chọn |
| `VISION_OCR_CHAR_DICT` | unset | TensorRT CTC dictionary path, bắt buộc khi TRT |
| `VISION_OCR_DET_INPUT_HEIGHT` | `960` | TensorRT det input |
| `VISION_OCR_DET_INPUT_WIDTH` | `960` | TensorRT det input |
| `VISION_OCR_REC_IMAGE_HEIGHT` | `48` | TensorRT rec input |
| `VISION_OCR_REC_IMAGE_WIDTH` | `320` | TensorRT rec input |
| `VISION_OCR_DET_THRESHOLD` | `0.30` | DB probability bitmap threshold |
| `VISION_OCR_DET_BOX_THRESHOLD` | `0.60` | contour mean probability gate |
| `VISION_OCR_DET_MIN_BOX_SIZE` | `3` | min det box dimension |

### Barcode, business rules, quality và scoring

| Env | Default | Consumer/ý nghĩa |
|---|---:|---|
| `VISION_BARCODE_ENGINE` | `zxing` | V1 chỉ chấp nhận `zxing` |
| `VISION_REQUIRED_FIELDS` | `sku` | comma-separated; pipeline luôn extract SKU và LOT nhưng chỉ field này bắt buộc theo default |
| `VISION_BARCODE_REQUIRED` | `false` | barcode missing trở thành `REVIEW` nếu true |
| `VISION_QUALITY_MIN_WIDTH` | `32` | crop min width |
| `VISION_QUALITY_MIN_HEIGHT` | `16` | crop min height |
| `VISION_QUALITY_MIN_SHARPNESS` | `50` | Laplacian variance threshold |
| `VISION_QUALITY_MIN_BRIGHTNESS` | `20` | low-light threshold |
| `VISION_QUALITY_MAX_BRIGHTNESS` | `245` | overexposure mean threshold |
| `VISION_QUALITY_MAX_UNDEREXPOSED_RATIO` | `0.30` | dark-pixel ratio |
| `VISION_QUALITY_MAX_OVEREXPOSED_RATIO` | `0.30` | bright-pixel ratio |
| `VISION_QUALITY_MAX_GLARE_RATIO` | `0.20` | glare-pixel ratio |
| `VISION_SCORE_SHARPNESS_REFERENCE` | `500` | normalize crop sharpness |
| `VISION_SCORE_WEIGHT_DETECTION` | `0.25` | candidate score weight |
| `VISION_SCORE_WEIGHT_SHARPNESS` | `0.35` | candidate score weight |
| `VISION_SCORE_WEIGHT_EXPOSURE` | `0.15` | candidate score weight |
| `VISION_SCORE_WEIGHT_AREA` | `0.05` | candidate score weight |
| `VISION_SCORE_WEIGHT_FRESHNESS` | `0.05` | candidate score weight |
| `VISION_SCORE_WEIGHT_GLARE` | `0.05` | candidate score weight |
| `VISION_SCORE_WEIGHT_VALIDITY` | `0.10` | invalid-crop penalty/weight |
| `VISION_LOG_LEVEL` | `INFO` | CLI logging |

`Settings.validate()` kiểm tra buffer/top-K, preview range, OCR confidence,
RTSP timeout, detector/OCR/barcode enum, FixedROI format, TensorRT required
paths/dimensions/thresholds, quality ratios và score weights. Nó chưa là
Pydantic schema; malformed numeric env có thể raise `ValueError` ngay trong
module import trước khi CLI xử lý được.

CLI flags có precedence riêng:

- `--source` > `VISION_RTSP_URL`.
- `--roi` thay `VISION_LABEL_ROI`; `--roi-absolute` thay normalized flag.
- Các script inspection có `--detector` default là `fixed-roi`, do đó CLI
  default có thể override `VISION_DETECTOR` nếu user không truyền flag.

## 6. Dependency và external-service map

### Python/package dependencies

| Group | Packages | Vai trò |
|---|---|---|
| Base | `numpy>=1.26`, `opencv-python-headless>=4.8`, `python-dotenv>=1.0` | image arrays, OpenCV capture/processing, `.env` |
| `.[dev]` | `pytest>=8.0` | tests |
| `.[ocr]` | `paddleocr>=3.0` | OCR adapter; PaddlePaddle binary phải cài riêng theo OS/CUDA |
| `.[ocr-transformers]` | `paddleocr>=3.0`, `transformers>=5.8.0` | PP-OCRv6 Transformers path trên GX10 |
| `.[ocr-tensorrt]` | `tensorrt-cu13`, `cuda-python`, `cuda-bindings` | native TensorRT backend; target-specific |
| `.[barcode]` | `zxing-cpp>=2.3` | barcode decoder |
| `.[detector]` | `torch>=2.2`, `ultralytics>=8.2` | deferred custom detector |
| `.[transport]` | `redis>=5.0` | **không được import bởi source hiện tại; roadmap only** |

Không có lockfile, requirements pin đầy đủ, Dockerfile hay deployment profile.
`pydantic/pydantic-settings` chỉ xuất hiện trong historical plan, không phải
dependency/code hiện tại.

### External services/runtime

- **RTSP camera**: URL do env/CLI cung cấp; OpenCV/FFmpeg kết nối trực tiếp.
- **PP-OCR**: local PaddleOCR + PaddlePaddle, model lifecycle resident trong
  `PPOCRAdapter`; không có model download/configuration code trong repo.
- **TensorRT/CUDA**: engine binary và char dictionary phải tồn tại trên target;
  engine cần build trên đúng GPU, không copy artifact Mac/x86 sang GX10.
- **ZXing-C++**: local native binding; adapter xử lý missing/link/runtime error.
- **Ultralytics**: local Torch/YOLO; model custom chưa có, license chưa được
  chốt cho commercial use.
- **Redis/GLM-OCR/cloud/ERP/PLC/MinIO/PostgreSQL**: không có active integration.

Heavy dependencies được giữ sau adapter boundary: Paddle/ZXing/Ultralytics
được import khi cần; TensorRT module import NumPy ở top-level nhưng không import
TensorRT/CUDA cho tới khi runner được khởi tạo. `app.py` import class adapters
nhưng không load model trong `build_pipeline()` ngoại trừ Ultralytics constructor
khi detector đó được chọn.

## 7. Test map và verification status

`pyproject.toml` cấu hình `tests/`, `test_*.py`, và markers:
`integration`, `runtime`, `requires_paddle`, `requires_zxing`, `requires_rtsp`.

| Area | Tests |
|---|---|
| Contract/JSON | `test_schema.py`, `test_pipeline_contract.py` |
| Buffer/selection | `test_frame_buffer.py`, `test_selector.py`, `test_candidate_ranking.py` |
| Camera | `test_rtsp.py`, `test_camera_hardening.py`, runtime RTSP test |
| Detection | `test_detection.py`, `test_ultralytics_detector.py` |
| Crop/quality | `test_crop_rectify.py`, `test_quality.py` |
| OCR/barcode | `test_ocr_parser.py`, `test_tensorrt_ocr.py`, `test_barcode.py` |
| Business logic | `test_field_extraction.py`, `test_validation.py` |
| Config/runtime/smoke | `test_config_wiring.py`, `test_runtime_readiness.py`, `test_smoke.py` |

Recommended commands từ README:

```bash
python -m pytest -q
python -m pytest -q -m "not integration and not runtime"
python -m pytest -q -m integration
python -m pytest -q -m runtime
python scripts/check_runtime.py
```

Verification trên host khảo sát ngày `2026-08-22`:

- `git status` trước khi tạo document sạch, branch `main` tại commit
  `1c400bf` (`Support modern CUDA Python runtime bindings`).
- Python `3.11.9`, `AMD64`: nằm trong range `>=3.10,<3.13`.
- `python -m pytest -q`: **không chạy được** vì host thiếu module `pytest`.
- `python scripts/benchmark_selector.py ...`: **không chạy được** vì host thiếu
  `numpy`.
- `python scripts/check_runtime.py`: Python `PASS`, nhưng OpenCV, NumPy,
  ZXing-C++, Paddle, PaddleOCR đều `FAIL` do `ModuleNotFoundError`; RTSP source
  `INFO: not configured`; detector Torch không được chọn.
- Không có claim nào ở đây rằng PP-OCR, ZXing, RTSP, TensorRT hoặc GX10 đã
  runtime-verified. `tasks/todo.md` có ghi một lần verify cũ, nhưng không thể
  tái hiện trên host hiện tại.

## 8. Runtime assumptions và coupling quan trọng

### Data assumptions

1. Input frame thường là OpenCV BGR `numpy.ndarray` dạng `H x W x C`; nhiều
   helper chỉ cần `.shape` và slicing `frame[y1:y2, x1:x2]`.
2. Candidate bbox là tọa độ absolute theo frame hiện tại. Chỉ FixedROI input
   mới có thể normalized.
3. Selection được chạy trên preview dài `320..640`; detection, crop, rectify,
   quality, OCR và barcode dùng original full-resolution frame/crop.
4. `FramePacket` cần wall-clock `captured_at`; freshness ưu tiên
   `captured_monotonic` nếu có.
5. Pipeline xử lý một invocation tuần tự và materialize packet iterable; không
   có worker pool/backpressure trong current implementation.

### Coupling graph

```text
Settings/.env
   -> app.build_pipeline
       -> detector + OCR + barcode adapters
       -> FrameSelector + CandidateScorer + QualityChecker
       -> FieldExtractor + LabelValidator

Camera/Video
   -> FramePacket
   -> FrameBuffer
   -> FrameSelector
   -> InspectionPipeline

LabelDetector
   -> LabelCandidate(bbox,corners)
   -> crop/rectify
   -> QualityReport + LabelCandidateScore
   -> selected image

OCRProvider
   -> RawOCRResult.lines
   -> FieldExtractor
   -> ExtractedField
   -> LabelValidator

BarcodeDecoder
   -> BarcodeResult
   -> LabelValidator

all stages
   -> InspectionResult
   -> to_dict (JSON contract)
```

Các coupling không được bỏ qua khi sửa code:

- `schemas.py` là contract chung; thay field/state ảnh hưởng adapter, pipeline,
  tests và consumer JSON.
- `app.py` là wiring duy nhất cho production-like pipeline; test thường bypass
  nó để inject fake adapter.
- `InspectionPipeline` phụ thuộc vào candidate coordinates, quality reason
  strings và stage states để quyết định downstream execution/status.
- `VISION_OCR_CONFIDENCE` được wire vào validator, không phải Paddle OCR
  inference threshold.
- Detector device và OCR device độc lập; Torch CUDA availability không suy ra
  Paddle CUDA availability.
- `Settings()` đọc environment ở import time; test/process muốn đổi env phải
  tạo `Settings()` mới hoặc reload module.
- Optional native adapters phải không làm package import thất bại trong unit
  environment; riêng TensorRT OCR có hard dependency NumPy (base dependency).

## 9. Known issues, deferred work và risks

### Đã biết từ implementation

- **Host chưa cài dependencies**: không thể dùng test suite/benchmark/runtime
  check làm bằng chứng PASS trên máy hiện tại.
- **Không có reproducible dependency lock**: optional OCR cần PaddlePaddle
  binary tương thích target nhưng package extra không tự cài package đó.
- **No target acceptance evidence**: chất lượng/latency p50/p95, camera FPS/
  resolution, OCR accuracy, barcode coverage và GX10 CUDA memory chưa được đo
  trong repo.
- **Quality defaults provisional**: các ngưỡng trong `.env.example` chưa được
  calibrate bằng capture camera thật.
- **RTSP cancellation backend-specific**: `CameraAcquisition.stop()` bounded
  join, nhưng native `capture.read()` có thể còn chạy trong daemon thread sau
  timeout.
- **Frame window/config mismatch**: RTSP CLI truyền `VISION_BUFFER_WINDOW_MS`
  vào `FrameBuffer`; replay helper dùng constructor default `800` thay vì đọc
  setting window. `Settings.validate()` cũng chưa validate `buffer_window_ms`.
- **CLI detector precedence**: `--detector` default `fixed-roi` ở các inspection
  script có thể override `VISION_DETECTOR` dù user không gõ flag.
- **`check_runtime.py` là readiness diagnostic**: chạy mặc định sẽ fail nếu
  optional PP-OCR/ZXing chưa cài, dù unit-level package code vẫn import được.
- **Top-level result error hạn chế**: stage failure details nằm trong
  `raw_ocr`, `barcode` và `validation.reasons`; `InspectionResult.error` chủ yếu
  được set cho no-frame/detection/crop/quality early return.
- **TensorRT chỉ có contract/unit coverage**: chưa có test thật cho serialized
  engine, CUDA allocation, DB output shape, CTC dictionary hay classifier trên
  target.
- **Detector scope risk**: Contour có thể được bật qua config nhưng không được
  chấp nhận cho GX10 V1; Ultralytics có thể được instantiate qua config nhưng
  model/license/accuracy chưa production-ready.

### Chưa có trong code (roadmap, không được coi là existing behavior)

- alternate preprocessing + multi-frame retry ladder;
- GLM-OCR client/sidecar, timeout/circuit breaker/structured fallback;
- model health/version reporting;
- broker abstraction, Redis Streams ACK/PENDING/retry/idempotency/DLQ;
- Vision Worker process/service;
- custom `shipping_label` detector training/validation;
- Docker/deployment profiles/license inventory.

## 10. Invariants không được phá

Mọi implementation task sau phải giữ các invariant này hoặc cập nhật document
và contract tests trước:

1. **Isolation**: không import hoặc sửa legacy project; package V2 vẫn standalone.
2. **Bounded memory**: frame buffer luôn bounded (`deque(maxlen=...)`); không
   đẩy raw frame bytes vào Redis/queue nếu transport về sau được thêm.
3. **Freshness**: stale frame không được chạy detection/OCR; ưu tiên monotonic
   timestamp khi packet có timestamp đó.
4. **Top-K semantics**: Top-K không đồng nghĩa OCR K lần; V1 mặc định chỉ một
   selected crop đi qua OCR/barcode.
5. **Full-resolution evidence**: preview chỉ dùng để chọn frame; không thay
   original frame bằng preview cho detection/crop/OCR/barcode.
6. **Crop safety**: bbox phải clamp vào frame, giữ `source_bbox`/provenance và
   không tạo empty crop; rectify chỉ áp dụng khi có đúng bốn corners hợp lệ.
7. **Independent evidence**: `raw_ocr.lines` phải giữ nguyên evidence OCR;
   `extracted` là lớp parse SKU/LOT riêng, không ghi đè raw text.
8. **Stage states**: stage chỉ dùng `NOT_RUN`, `SUCCESS`, `FAILED`; stage chưa
   chạy phải thể hiện `NOT_RUN`, không giả thành failure/success đã chạy.
9. **Validation safety**: OCR/barcode/quality runtime failure không thể trở
   thành unvalidated `PASS`; low-confidence required field phải ít nhất
   `REVIEW`.
10. **Error boundary**: adapter exception phải trở thành structured result với
    mã tổng quát; không leak secret, model path hoặc exception detail ra JSON.
11. **Barcode independence**: barcode decoding độc lập với OCR; barcode thành
    công không che giấu OCR failure và ngược lại.
12. **Resident lifecycle**: PaddleOCR/YOLO/TensorRT model không load lại mỗi
    frame; giữ adapter/model resident trong process.
13. **Device separation**: không suy luận Paddle GPU readiness từ Torch; giữ
    `VISION_DETECTOR_DEVICE` và `VISION_OCR_DEVICE` độc lập.
14. **JSON safety**: `InspectionResult.to_dict()` phải serialize được bằng
    `json.dumps`; position/polygon phải normalize thành primitive JSON types.
15. **Secret hygiene**: RTSP credentials chỉ dùng cho connection; mọi
    application-owned log/error/output phải mask password.
16. **V1 detector scope**: FixedROI remains the supported GX10 V1 path until
    camera dataset, detector accuracy và license của detector thay thế được
    chấp nhận rõ ràng.
17. **Exit semantics**: completed business decision (`PASS`, `FAIL`, `REVIEW`)
    không bị biến thành process failure; capture/pipeline/runtime/config failure
    mới trả non-zero theo `smoke.py` contract.

## 10.1 Current GX10 runtime integration slice (2026-08-22)

The GX10 acceptance path now has an explicit resident PP-OCRv6 Transformers
adapter. It is selected only with `VISION_OCR_ENGINE=ppocr_v6`,
`VISION_OCR_BACKEND=transformers`, and `VISION_OCR_VERSION=PP-OCRv6`.
The adapter constructs `PaddleOCR` with the required document-orientation and
unwarping flags disabled, and loads the predictor once per process. It returns
`RawOCRResult` with engine/backend/device/model metadata, raw OCR lines, and
JSON-safe state/status fields.

`app.build_pipeline()` remains the only production wiring point. The GX10 path
uses FixedROI, ZXing-C++, and one selected crop for OCR/barcode. Required fields
are appended to the evidence extractor, so the Shopee profile can validate
`tracking_number` and `order_id` while preserving raw OCR separately. The
minimal profile recognizes the observed `SPX...` tracking shape and the
alphanumeric order-id shape; it does not invent SKU or LOT values.

Real-runtime entrypoints:

- `scripts/test_zxing_runtime.py`: real ZXing-C++ decode with primitive JSON
  positions, number/text/format/valid fields, and latency.
- `scripts/test_ppocr_v6.py`: real PP-OCRv6 Transformers inference, warmup,
  and p50/p95 timing with model load excluded from timed runs.
- `scripts/run_real_image_integration.py`: real image
  FixedROI -> quality -> PP-OCRv6 -> ZXing -> extraction/validation and a
  20-run warm benchmark. It stops before benchmarking when the initial image
  result is not `PASS`.
- `scripts/run_real_rtsp_integration.py`: real RTSP acquisition, bounded
  buffer/stale accounting, selected frame/crop score, and the same OCR/barcode
  pipeline. Run it only after image integration has passed.
- `scripts/manual_rtsp_inspection.py`: direct Android IP Cam RTSP/HTTP URL,
  `CameraAcquisition` -> `FrameBuffer`, manual Enter/timed trigger, then the
  same Top-K -> FixedROI -> PP-OCRv6 -> ZXing -> JSON path. It is the POC path
  for a phone camera and does not require MediaMTX.

The repository does not claim GX10 runtime completion from unit or mock tests.
`python -m compileall -q src scripts tests` is only a local syntax check; the
current Windows host lacks `pytest`, CUDA/PaddleOCR, and GX10 image/RTSP
assets, so real verification must be performed on GX10.

Scope remains frozen for this slice: ONNX, Paddle2ONNX, TensorRT, GLM-OCR,
Redis, GigE, custom YOLO/training, ERP, and scale-out changes. Any later
fallback from Transformers to ONNX/TensorRT requires a separate
benchmark-backed decision.

## 11. Hướng dẫn cho task tiếp theo

Trước khi sửa code, đọc theo thứ tự:

1. tài liệu này;
2. `README.md` và `pyproject.toml`;
3. `config.py` + `schemas.py`;
4. `app.py` + `pipeline/inspection.py`;
5. module boundary liên quan và test contract tương ứng.

Nếu task chạm vào một invariant hoặc một field trong `schemas.py`, cần cập nhật
test trước/đồng thời và cập nhật document này. Nếu task muốn bật một phần đang
deferred, cần ghi rõ runtime target, artifact/dependency/license evidence và
không suy ra rằng code tồn tại trong historical `tasks/plan.md` nghĩa là code
đã active.
