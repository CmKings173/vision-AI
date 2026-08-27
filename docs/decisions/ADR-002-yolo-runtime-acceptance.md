# ADR-002: Keep the trained YOLO detector experimental until live acceptance

## Status

Accepted for the runtime-acceptance remediation; production promotion pending.

## Context

The GX10 image smoke proved that the trained checkpoint can produce a useful
label box, but the previous direct-camera run produced repeated
`LABEL_NOT_DETECTED` results and stale-frame attempts. A model-load success or
one end-to-end pass is therefore not sufficient evidence for production
detector support.

## Decision

- Keep `UltralyticsLabelDetector.support_level` as `EXPERIMENTAL` until a
  controlled live acceptance run supplies detection-attempt evidence.
- Require the configured checkpoint to exist, record its SHA-256, and require
  the `shipping_label` class in its schema.
- Verify and record the actual execution device before warmup/inference; CUDA
  configuration fails closed when the requested device is unavailable.
- Persist the exact detector input and raw/accepted detections for every fresh
  trigger, including detector misses.
- Report detector, OCR, and successful end-to-end metrics using separate
  denominators. An OCR `NOT_RUN` stage is not a zero-latency observation.
- After a stale trigger, wait for camera recovery and a fresh frame before
  accepting the next trigger; do not emit a timed stream of known-stale
  attempts.

## Alternatives considered

### Keep YOLO as supported after the image smoke

Rejected: image smoke does not measure camera orientation, stream stability,
live detection recall, or stale-frame behavior.

### Lower confidence or change the ML strategy automatically

Rejected: this remediation is for runtime evidence and observability. Threshold
changes are explicit CLI/config overrides and diagnosis-only offline probes.

### Fall back to FixedROI when YOLO misses

Rejected: that would hide detector misses and change the acceptance flow.

## Consequences

The detector is usable for controlled diagnosis and acceptance experiments, but
the application must not claim production detector support yet. Runtime logs
and event artifacts are larger because they preserve detector inputs and
metadata. The checkpoint hash, class mapping, thresholds, and actual device
make each result reproducible and auditable.
