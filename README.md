# Vision AI V2 — OCR/Label Inspection

This is a new, isolated inspection pipeline. The legacy project at
`/Users/boss/Projects/vison-AI-server` is not imported or modified.

## V1 target

```text
image/video/phone RTSP
  → bounded + stale-safe frame buffer
  → global pre-rank top-K
  → detect/crop/quality-score every top-K candidate
  → select one best label crop
  → ZXing barcode + PP-OCR
  → raw OCR lines
  → SKU/LOT field extraction
  → deterministic validation
  → structured JSON
```

The current implementation keeps optional heavyweight integrations behind
adapters. GLM-OCR, custom YOLO and Redis Streams remain intentionally deferred.
Top-K does not mean OCR K times: only the highest-ranked crop reaches PP-OCR
and ZXing in V1.

For GX10 V1 acceptance, `FixedROI` is the only supported detector. `Contour`
is experimental, and the Ultralytics/custom-YOLO adapter is deferred. The
Ultralytics licensing/commercial-use decision is a release blocker before any
commercial deployment.

## Runtime requirements

- Python `>=3.10,<3.13`; Python 3.11 is recommended for GX10.
- `VISION_LABEL_ROI=x1,y1,x2,y2` is mandatory in `fixed-roi` mode.
- `VISION_DETECTOR_DEVICE` and `VISION_OCR_DEVICE` are framework-specific.
- `VISION_OCR_CONFIDENCE` is a validation threshold: low-confidence lines stay
  in `raw_ocr.lines` and extracted fields, but force `REVIEW` when required.
- Quality thresholds in `.env.example` are provisional engineering defaults,
  not camera-calibrated production values.
- `VISION_FRAME_PREVIEW_LONG_EDGE` must be 320–640; selection runs on that
  preview while detection, crop, OCR and barcode always use the original frame.

Check readiness without installing anything:

```bash
python3 scripts/check_runtime.py
```

## Development

```bash
cd /Users/boss/Projects/vision-AI
python3 -m pytest -q
python3 scripts/benchmark_selector.py --frames 8 --width 3840 --height 2160 --preview-long-edge 480
python3 scripts/inspect_image.py --image /path/to/label.jpg --detector fixed-roi --roi 0.1,0.1,0.9,0.9
python3 scripts/replay_video.py --source /path/to/video.mp4 --max-frames 20 --pipeline --roi 0.1,0.1,0.9,0.9
python3 scripts/inspect_rtsp.py --max-frames 30 --roi 0.1,0.1,0.9,0.9
python3 scripts/camera_smoke.py --timeout-s 10
```

Set `VISION_RTSP_URL` in the local environment or `.env`; `--source` is only
an explicit override. Application-owned logs mask embedded passwords.

For a PaddleOCR run install the optional packages in the target environment:

```bash
pip install -e '.[ocr,barcode]'
```

The OCR group also needs a PaddlePaddle CPU/GPU build compatible with the
target OS and CUDA runtime.

## GX10 real PP-OCRv6 Transformers path (preferred)

The GX10 acceptance path uses the already validated PyTorch CUDA runtime,
PP-OCRv6 through PaddleOCR's `transformers` engine, FixedROI, and real
ZXing-C++. Do not install a second Torch build over the GX10 image. Install
the project adapter dependencies in the existing Python 3.11 environment:

```bash
cd ~/Projects/vision-AI
source .venv/bin/activate
python -m pip install -e '.[ocr-transformers,barcode]'

export VISION_OCR_ENGINE=ppocr_v6
export VISION_OCR_BACKEND=transformers
export VISION_OCR_VERSION=PP-OCRv6
export VISION_OCR_DEVICE=gpu:0
export VISION_BARCODE_ENGINE=zxing

python scripts/check_runtime.py
python scripts/test_zxing_runtime.py \
  --image /home/minh/Projects/vision-AI/test_data/label_crop.jpg
python scripts/test_ppocr_v6.py \
  --image /home/minh/Projects/vision-AI/test_data/label_crop.jpg \
  --device gpu:0 --runs 20
```

Run the complete real image path before touching RTSP. The default full-frame
ROI is still FixedROI; pass the calibrated normalized ROI when `pic.jpg`
contains background around the label:

```bash
python scripts/run_real_image_integration.py \
  --image /home/minh/Projects/vision-AI/test_data/pic.jpg \
  --roi 0,0,1,1 \
  --device gpu:0 \
  --required-fields tracking_number,order_id \
  --warmup 2 --runs 20
```

Only when that command returns image `PASS`, run the direct phone-camera path.
The existing IP Cam app supplies the URL; no MediaMTX or intermediate relay is
needed. `RTSPCamera` passes the URL to OpenCV, so both RTSP and HTTP camera
URLs are accepted when the GX10 OpenCV/FFmpeg build supports that stream:

```bash
export VISION_RTSP_URL='rtsp://PHONE_IP:PORT/PATH'
# Or, for an HTTP/MJPEG endpoint:
# export VISION_RTSP_URL='http://PHONE_IP:PORT/PATH'
export OPENCV_FFMPEG_CAPTURE_OPTIONS='rtsp_transport;tcp'
# Replace these calibration values from the saved selected frame. Full-frame
# 0,0,1,1 is rejected by this acceptance entrypoint.
export VISION_LABEL_ROI='LABEL_X1,LABEL_Y1,LABEL_X2,LABEL_Y2'
export VISION_CAMERA_ROTATE_DEG=0
python scripts/manual_rtsp_inspection.py \
  --source "$VISION_RTSP_URL" \
  --roi "$VISION_LABEL_ROI" \
  --rotate-deg "$VISION_CAMERA_ROTATE_DEG" \
  --device gpu:0 \
  --triggers 10 \
  --debug-dir artifacts/manual_rtsp_inspection
```

The command loads and warms PP-OCRv6, imports/prepares ZXing, then connects the
camera. It prints `SYSTEM READY` only after OCR, ZXing, and a fresh camera frame
are ready. Each Enter keeps the same process and resident model, snapshots the
fresh ring-buffer frames, ranks Top-K, normalizes the configured orientation,
applies the calibrated FixedROI, and runs one PP-OCRv6 + ZXing pass. It repeats
for 10 triggers and prints p50/p95 after the loop; model load/warmup is excluded
from `ocr_ms`.

Each completed event is stored under its event ID:

```text
artifacts/manual_rtsp_inspection/<event_id>/
  selected_frame.jpg
  label_crop.jpg
  result.json
```

`selected_frame.jpg` is the oriented frame passed to FixedROI/OCR, and
`label_crop.jpg` is the crop passed to OCR/ZXing. Use the first saved frame to
calibrate `VISION_LABEL_ROI` and confirm whether `VISION_CAMERA_ROTATE_DEG`
must be `90`, `180`, or `270` before the 10-trigger acceptance run. Use a larger
window when phone Wi-Fi jitter makes the latest frames stale:

```bash
export VISION_BUFFER_WINDOW_MS=2000
```

For a non-interactive timing test, add `--trigger-after-s 5`. To verify only
the direct URL before running the full pipeline, use:

```bash
python scripts/camera_smoke.py --source "$VISION_RTSP_URL" \
  --max-frames 10 --timeout-s 15
```

These commands print JSON evidence for OCR lines, the DGX Spark label fields
(`customer_part_number`, `so_number`, `our_part_number`, `quantity`,
`net_weight`, `gross_weight`, `carton_number`), barcode format/value/validity/
position, selected frame/crop score, artifact paths, and timings. The 10-trigger
benchmark excludes model load/warmup time. Unit or mock tests do not
count as GX10 verification. ONNX, Paddle2ONNX, TensorRT, GLM-OCR, Redis,
GigE, custom YOLO/training, ERP, and scale changes are frozen in this path.

## GX10 native TensorRT OCR

The V2 source also contains a native TensorRT PP-OCR adapter. It does not
import PaddlePaddle and keeps the TensorRT detection, recognition, and
optional angle-classification engines resident in one process. The engines
must be built on the target GPU; a TensorRT engine built on Mac or x86 should
not be copied to GX10.

Install the target runtime inside the GX10 virtual environment:

```bash
python -m pip install -e '.[ocr-tensorrt]'
python scripts/check_runtime.py
```

Prepare PP-OCR ONNX models and build target-specific engines on GX10:

```bash
python scripts/build_tensorrt_engine.py \
  --onnx models/ppocr/det.onnx \
  --engine models/ppocr/det.engine \
  --shape 1,3,960,960 --fp16

python scripts/build_tensorrt_engine.py \
  --onnx models/ppocr/rec.onnx \
  --engine models/ppocr/rec.engine \
  --shape 1,3,48,320 --fp16
```

Set the OCR backend and model paths:

```bash
export VISION_OCR_ENGINE=tensorrt
export VISION_OCR_DEVICE=cuda:0
export VISION_OCR_DET_ENGINE=models/ppocr/det.engine
export VISION_OCR_REC_ENGINE=models/ppocr/rec.engine
export VISION_OCR_CHAR_DICT=models/ppocr/ppocr_keys_v1.txt
```

The adapter currently expects the standard split PP-OCR DB detection and CTC
recognition outputs. It keeps raw OCR lines in the existing result schema;
FieldExtractor, ZXing, validation, and timing remain unchanged.

Detector and transport dependencies are independent and are not needed for
the FixedROI + PP-OCR + ZXing vertical slice. Install `.[detector]` only for
the deferred Ultralytics adapter; `.[transport]` remains unused during the V1
freeze.

## Test levels

```bash
# Unit tests, no external OCR/barcode/RTSP runtime
python3 -m pytest -q -m "not integration and not runtime"

# Local integration with actual NumPy/OpenCV images and mocked model boundary
python3 -m pytest -q -m integration

# Runtime tests; missing dependencies/sources are explicitly skipped
python3 -m pytest -q -m runtime
```

Runtime test PASS is not equivalent to production verification. GX10 GPU,
phone RTSP, OCR accuracy, latency p50/p95, and camera-specific quality
thresholds must still be measured on the target system.

RTSP reads run on an independent daemon and the controller has a bounded wait.
The manual direct-camera entrypoint uses the same daemon and bounded
`FrameBuffer`; it does not add MediaMTX. OpenCV/FFmpeg native-read cancellation
after `release()` is backend-specific
and must still be verified on GX10 under packet loss and camera disconnects.
Smoke exit codes are deterministic: `0` completed (including REVIEW/FAIL
business decisions), `1` capture/pipeline runtime failure, and `2` invalid or
unsupported local runtime/configuration.

PP-OCR, ZXing, phone RTSP and GX10 are not claimed verified until their runtime
tests and target acceptance dataset have actually passed.

## Result contract

Every inspection keeps both `raw_ocr.lines` and `extracted` fields so an OCR
mistake can be distinguished from a SKU/LOT parsing mistake.
