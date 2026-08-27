# YOLO runtime acceptance contract

This document is the current operational contract for the explicit
`--detector yolo` path. It supersedes older notes that described the adapter
only as a deferred stub.

## Current status

`UltralyticsLabelDetector` is `EXPERIMENTAL`. The GX10 image smoke proves only
that the checkpoint can load and infer on a saved image. Live camera detection
recall, stale recovery, and 50-attempt runtime acceptance are separate evidence
requirements.

## Startup gate

Before `SYSTEM READY`, the process must have:

- loaded the exact configured checkpoint path;
- recorded checkpoint SHA-256 and class mapping;
- verified that the class schema contains `shipping_label`;
- verified the actual device (`cpu` or `cuda:<index>`), rather than trusting the
  configured alias;
- warmed the resident detector, PP-OCRv6, and ZXing runtimes.

The process fails closed for a missing checkpoint, incompatible class schema,
or unavailable CUDA device. A generic `yolo26s.pt` checkpoint is not silently
substituted for the trained checkpoint.

## Per-trigger evidence

For each trigger with a usable fresh frame, the manual RTSP path writes:

```text
<debug-dir>/<event_id>/
  selected_frame.jpg
  detector_input.jpg
  detector_debug.json
  label_crop.jpg       # only after an accepted label detection
  result.json
```

`detector_input.jpg` is a full-resolution image actually passed to YOLO.
`detector_debug.json` contains event/frame IDs, selected Top-K candidates,
checkpoint identity, actual device, thresholds, inference duration, raw
detections, and accepted detections. Detector misses are persisted too.

## Metrics

The final summary keeps separate denominators:

- `detection_attempts`, `detection_hits`, `detection_misses` and detection
  p50/p95 use detector calls only;
- `ocr_attempts`, `ocr_successes`, `ocr_failures` and OCR p50/p95 include only
  OCR stages that executed;
- `full_pipeline_attempts` and `full_pipeline_passes` are independent from
  detector-only attempts;
- successful E2E p50/p95 includes only full pipeline PASS observations;
- `stale_trigger_count` is reported separately and stale triggers do not create
  fake OCR zero-latency samples.

After `NO_FRESH_FRAME_AT_TRIGGER`, the loop waits for camera recovery and a
fresh frame. It does not continue emitting timed stale attempts.

## Offline diagnosis

Use the exact saved input and exact trained checkpoint:

```bash
python scripts/debug_yolo_detector.py \
  --image artifacts/manual_rtsp_yolo/<event_id>/detector_input.jpg \
  --weights /path/to/training/runs/.../best.pt \
  --device gpu:0 \
  --confidence 0.25 --iou 0.45 --imgsz 640 --max-det 10 \
  --output-json artifacts/manual_rtsp_yolo/<event_id>/offline_detector_debug.json
```

This is diagnosis only. It does not lower thresholds automatically, invoke
FixedROI as a fallback, or claim production accuracy.
