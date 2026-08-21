# Implementation Plan: Vision AI V2 — OCR/Label Inspection

## Overview

Build a new, isolated OCR/Label Inspection project in `/Users/boss/Projects/vision-AI`. The existing `/Users/boss/Projects/vison-AI-server` source remains untouched and continues to serve the current robot vision flow.

V2 will target the first usable flow:

```text
RTSP camera → bounded frame buffer → top-K frame selection
→ label detection → padded crop → optional rectification
→ quality check → PP-OCR + barcode → field extraction
→ deterministic validation → structured JSON
```

GLM-OCR is an optional fallback, preferably deployed as a separate resident sidecar because the current source's vLLM/Transformers/CUDA stack is not the same as the current GLM-OCR self-host requirements. Redis is optional for the first local pipeline and, when introduced, should use a broker abstraction with Redis Streams semantics rather than copying the old `LPUSH/BRPOP` behavior.

## Feasibility Decision

**Decision: GO.**

Reuse is high for camera acquisition, frame synchronization patterns, configuration conventions, YOLO inference mechanics, debug output, and smoke-test patterns. Reuse is low for the business CV pipeline because the existing YOLO model is COCO-trained and the repository has no OCR or barcode implementation.

## Architecture Decisions

- Keep V2 outside the old repository: no imports from the old application at runtime and no edits to `/Users/boss/Projects/vison-AI-server`.
- Use a camera protocol with RTSP first; add USB and GigE adapters without coupling the pipeline to OpenCV or a vendor SDK.
- Use a bounded timestamped ring buffer. Do not retain an unbounded stream or send raw frames through Redis.
- Select top-K recent frames using inexpensive quality signals, then score label candidates using detection confidence, crop quality, freshness, and label area. Do not assume the sharpest global frame contains the best label.
- Start with `FixedROI/ContourDetector` for a controlled camera setup. Add a custom `ShippingLabelYOLODetector` only after representative label data is available.
- Treat bbox padding and perspective correction as separate stages. A bbox alone does not provide four corners; rectification is conditional on reliable quadrilateral/keypoint evidence.
- Use PP-OCR as the fast local path. Add a deterministic `FieldExtractor` after OCR to map text lines into SKU/LOT/etc.; OCR output alone does not know business fields.
- Keep barcode decoding independent from OCR. Use ZXing-C++ as the main decoder and OpenCV-compatible preprocessing/fallbacks where useful.
- Use a fallback ladder: PP-OCR original crop → alternate preprocessing/second frame → optional GLM-OCR. GLM-OCR must not be the source of PASS/FAIL decisions.
- If self-hosted, run GLM-OCR as a separate resident service/sidecar. Do not combine it blindly with the old `vllm==0.7.3` and `transformers==4.49.0` environment.
- Use `PASS`, `FAIL`, `REVIEW`, and `ERROR` statuses with machine-readable reason codes.
- Measure stage timings with a monotonic high-resolution clock and include model/version metadata in the inspection result.
- Introduce Redis only after the in-process/image/video pipeline is testable. When needed, use a broker interface and Redis Streams with consumer groups, ACK, pending recovery, retry, and dead-letter handling.

## Reuse Map

### Reuse as reference or extracted logic

- `vison-AI-server/main.py`: RTSP open/reconnect, frame reader thread, lock-protected frame access, wait-for-first-frame behavior.
- `vison-AI-server/modules/Object_detection/object_detector.py`: tensor conversion, model lazy-load pattern, BGR/RGB handling, inference invocation, bbox parsing, CUDA fallback pattern.
- `vison-AI-server/modules/Object_detection/config.py`: environment-backed model/device/threshold conventions.
- `vison-AI-server/modules/Object_detection/utils.py`: drawing, JSON conversion, summaries, debug-report patterns.
- `vison-AI-server/scripts/object_detection_video_smoke.py`: image/video replay and sampled-frame smoke-test pattern.
- `vison-AI-server/config/redis_manager.py`: Redis connection/configuration conventions only; do not reuse its non-ACK `BRPOP` contract.

### Explicitly excluded from V2 runtime

- Gemini agent/tool routing.
- Face Recognition/DeepFace and face datasets.
- Qwen-VL captioning and old VLM verification.
- YOLOv9/YOLOv10 legacy experiments.
- Robot voice/agent transport code.
- PostgreSQL, MinIO, ERP, PLC, scale integration, and weighing-scale integration in V1.

## Target Folder Structure

```text
/Users/boss/Projects/vision-AI/
├── README.md
├── pyproject.toml
├── .env.example
├── tasks/
│   ├── plan.md
│   └── todo.md
├── src/label_inspection/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── schemas.py
│   ├── timing.py
│   ├── camera/
│   │   ├── base.py
│   │   ├── rtsp.py
│   │   ├── usb.py
│   │   ├── gige.py
│   │   ├── frame_buffer.py
│   │   └── selector.py
│   ├── detection/
│   │   ├── base.py
│   │   ├── fixed_roi.py
│   │   ├── contour.py
│   │   └── ultralytics_detector.py
│   ├── preprocessing/
│   │   ├── crop.py
│   │   ├── rectify.py
│   │   └── quality.py
│   ├── ocr/
│   │   ├── base.py
│   │   ├── ppocr.py
│   │   └── glm_ocr.py
│   ├── barcode/
│   │   ├── base.py
│   │   └── zxing.py
│   ├── extraction/
│   │   └── fields.py
│   ├── validation/
│   │   └── rules.py
│   ├── pipeline/
│   │   └── inspection.py
│   └── transport/
│       ├── broker.py
│       └── redis_streams.py
├── scripts/
│   ├── camera_smoke.py
│   ├── replay_video.py
│   └── inspect_image.py
└── tests/
    ├── fixtures/
    ├── test_schema.py
    ├── test_frame_buffer.py
    ├── test_selector.py
    ├── test_crop_rectify.py
    ├── test_quality.py
    ├── test_field_extraction.py
    ├── test_validation.py
    ├── test_pipeline_contract.py
    └── test_transport.py
```

## Task List

### Phase 0: Contract and isolated project foundation

- [ ] Task 1: Create the V2 project metadata, package skeleton, configuration contract, `.env.example`, and README.
- [ ] Task 2: Define Pydantic/domain schemas for frame packets, label candidates, OCR lines, barcode results, quality, validation, timing, and final inspection JSON.

### Checkpoint: Foundation

- [ ] `python -m pytest tests/test_schema.py -q` passes.
- [ ] Importing the package does not import DeepFace, vLLM, Gemini, or Qwen-VL.
- [ ] Source tree is under `/Users/boss/Projects/vision-AI` and old source has no diff.

### Phase 1: Camera and frame selection

- [ ] Task 3: Implement `CameraSource` protocol and RTSP reader with reconnect, bounded read loop, timestamped `FramePacket`, and clean shutdown.
- [ ] Task 4: Implement bounded ring buffer and top-K frame selector with freshness, sharpness, exposure, and optional ROI scoring.
- [ ] Task 5: Add image/video replay and camera smoke scripts.

### Checkpoint: Camera

- [ ] `python scripts/camera_smoke.py --source <rtsp-url>` opens/reconnects and reports frame metadata.
- [ ] `python scripts/replay_video.py --source <video-file> --max-frames 20` completes without unbounded memory growth.
- [ ] Unit tests cover empty buffer, stale frames, full buffer, and clean shutdown.

### Phase 2: Label candidate and crop quality

- [ ] Task 6: Implement detector protocol plus `FixedROIDetector` and contour-based V1 detector.
- [ ] Task 7: Extract/refactor Ultralytics adapter for a future `shipping_label.pt`; keep model loading resident and device-configurable.
- [ ] Task 8: Implement bbox padding, boundary clamp, crop provenance, optional quadrilateral detection, conditional perspective warp, and quality metrics.

### Checkpoint: Candidate preparation

- [ ] `python scripts/inspect_image.py --detector fixed-roi --image <image>` emits a label crop and quality report.
- [ ] Unit tests cover bbox edges, invalid boxes, no quadrilateral, valid quadrilateral, blur, low-light, and crop truncation.

### Phase 3: Barcode and OCR vertical slice

- [ ] Task 9: Implement barcode adapter with ZXing-C++ and preprocessing variants; preserve format, text, position, and validity.
- [ ] Task 10: Implement PP-OCR adapter with model load-once behavior and normalized OCR line output.
- [ ] Task 11: Implement `FieldExtractor` for SKU/LOT/etc. using anchors, regex, spatial grouping, and confidence aggregation.
- [ ] Task 12: Implement deterministic validation rules and PASS/FAIL/REVIEW/ERROR result generation.

### Checkpoint: Local inspection V1

- [ ] `python scripts/inspect_image.py --image <label-image>` returns valid structured JSON.
- [ ] Barcode and OCR can succeed/fail independently.
- [ ] Unit tests cover low OCR confidence, missing barcode, invalid regex, checksum failure, and readable PASS case.
- [ ] Timing includes frame selection, detection, crop/rectify, quality, OCR, barcode, field extraction, validation, and total.

### Phase 4: Multi-frame fallback and optional GLM-OCR

- [ ] Task 13: Add retry ladder: alternate crop preprocessing and second-best frame before GLM-OCR.
- [ ] Task 14: Define GLM-OCR client/sidecar adapter with timeout, circuit breaker, structured-output parsing, and no-GLM graceful degradation.
- [ ] Task 15: Add model lifecycle/health reporting and model version metadata.

### Checkpoint: Fallback behavior

- [ ] PP-OCR success does not invoke GLM-OCR.
- [ ] GLM-OCR timeout produces `REVIEW`/`ERROR`, never an unvalidated PASS.
- [ ] Sidecar/client tests run without the actual GLM model using mocks.

### Phase 5: Redis Streams worker

- [ ] Task 16: Define broker protocol and in-memory broker for tests.
- [ ] Task 17: Implement Redis Streams consumer group, ACK, retry/PENDING recovery, idempotency by `event_id`, and dead-letter handling.
- [ ] Task 18: Connect one Vision Worker that owns camera buffer and runs detector → crop → OCR → barcode in-process; send only job metadata and JSON results through Redis.

### Checkpoint: Worker integration

- [ ] `python -m label_inspection.worker --config .env.example` can run in dry-run/in-memory mode.
- [ ] Redis integration test confirms successful ACK, retry, duplicate event handling, and dead-letter behavior.
- [ ] Raw frame bytes never appear in Redis payloads.

### Phase 6: Custom detector and deployment hardening

- [ ] Task 19: Add `ShippingLabelYOLODetector` using a validated custom model and benchmark against FixedROI/ContourDetector.
- [ ] Task 20: Add deployment profiles for CPU, detector GPU + CPU PP-OCR, and detector/OCR GPU; record memory/latency benchmarks.
- [ ] Task 21: Add Docker/runtime documentation only after target CUDA/Paddle/GLM versions are fixed.

### Checkpoint: Complete

- [ ] Representative camera/video acceptance set meets accuracy and latency targets.
- [ ] All tests and smoke commands pass in the documented environment.
- [ ] Old source remains unchanged.
- [ ] Model, package, and license inventory is documented.

## Dependency Plan

### Base V2

- `numpy`
- `opencv-python-headless`
- `pydantic` / `pydantic-settings`
- `python-dotenv`
- `pytest`

### Optional detector/GPU group

- `torch`
- `ultralytics`

### Optional OCR group

- `paddleocr`
- compatible `paddlepaddle` or `paddlepaddle-gpu` selected for the target OS/CUDA.

### Optional barcode group

- `zxing-cpp`

### Optional transport group

- `redis`

### Explicitly not part of V2 base

- `deepface`, `tf-keras`
- `google-genai`
- `vllm`, `qwen-vl-utils`
- old `transformers` stack
- `torchvision`, `torchaudio`, unless a chosen model proves it needs them.

GLM-OCR must be pinned in a separate environment/container after choosing self-hosted or MaaS mode. Current official self-host examples use a newer vLLM/Transformers stack than the old project, so it should not be mixed into the old environment.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---:|---|
| Phone stream is too low-resolution for small text/barcode | High | Define minimum pixel height for text/barcode; keep full-resolution crop; require recapture when below threshold. |
| Fixed ROI fails when camera moves | High | Use it only as V1 controlled setup; add custom label detector after collecting data. |
| Best global frame does not contain best label | High | Select top-K and score detection/crop quality, not sharpness alone. |
| Bbox does not provide perspective corners | High | Make rectification conditional; use contour/keypoint/mask evidence. |
| PP-OCR and GLM-OCR dependency/CUDA conflict | High | Separate GLM-OCR sidecar/environment; pin versions per deployment target. |
| OCR text is not mapped to SKU/LOT correctly | High | Add deterministic FieldExtractor and regex/spatial rules with REVIEW state. |
| Barcode reader fails after aggressive preprocessing | Medium | Try original, grayscale, contrast, and rectified variants independently. |
| Redis job loss/reprocessing | Medium | Use Redis Streams, ACK, pending recovery, idempotency, and dead-letter queue. |
| GPU OOM from detector + OCR + GLM | High | Resident models only where required; limit concurrency; separate GLM sidecar; benchmark memory. |
| Model/package license mismatch | Medium | Inventory package/model/data licenses before commercial deployment. |
| Face data/model accidentally reused | Medium | Exclude face module and dataset from V2 packaging and runtime imports. |

## Open Questions

- What is the actual RTSP resolution and FPS from the phone/IP camera?
- Is the label position fixed in the camera view for V1?
- Which barcode formats are required: Code128, EAN-13, QR, Data Matrix, or others?
- What are the exact SKU/LOT field formats and validation rules?
- What minimum text height in pixels is acceptable?
- What are target p50/p95 latency and PASS accuracy?
- Must GLM-OCR run fully local, or is a cloud fallback permitted?
- What NVIDIA GPU/CUDA/Paddle deployment target will be used?
