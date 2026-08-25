# Phase 1 — Checkpoint 2 runtime handoff

## Scope and claim boundary

Checkpoint 2 runs the existing FixedROI → quality → PP-OCRv6 → ZXing →
FieldExtractor → LabelValidator path against an evaluation dataset. It does
not establish production accuracy by itself. The current target seed remains
`human_verified=false`, so a real one-image run is runtime smoke evidence only.

The working datasets are role-separated:

- `target`: the only role eligible for production accuracy;
- `robustness`: runtime/condition evidence only;
- `public`: inventory or external evaluation evidence only.

Within a target dataset, only human-verified, non-synthetic records enter the
accuracy denominator. A runtime failure remains in that denominator. Synthetic
and unverified samples still produce runtime, quality, ROI, latency, and
failure evidence but never a production accuracy claim.

## GX10 one-image smoke

Validate the dataset first and require `ok=true`:

```bash
python scripts/validate_eval_dataset.py \
  --dataset datasets/target/dgx_real_v1
```

Run one image with a calibrated label-only ROI. Full-frame `0,0,1,1` is not
accepted for DGX evaluation:

```bash
python scripts/evaluate_dataset.py \
  --dataset datasets/target/dgx_real_v1 \
  --split smoke \
  --image-id DGX_REAL_0001 \
  --device gpu:0 \
  --roi 0.10,0.10,0.90,0.90 \
  --rotate-deg 0 \
  --output results/dgx_real_v1
```

Replace the ROI and rotation with the already calibrated GX10 values. The
evaluator creates a unique immutable run directory below `--output`:

```text
results/dgx_real_v1/<run_id>/
├── provenance.json
├── summary.json
├── evaluation_report.md
├── field_metrics.csv
├── barcode_metrics.json
├── business_metrics.json
├── condition_metrics.csv
├── failures.csv
└── samples/<image_id>/
    ├── input.jpg
    ├── label_crop.jpg
    ├── prediction.json
    ├── ocr_lines.json
    ├── barcode.json
    └── stage_timings.json
```

`provenance.json` records the run ID, Git commit/dirty state, dataset and
configuration fingerprints, ROI, quality thresholds, dependency versions,
and extractor profile/mapping semantics. Reusing an existing run ID is
rejected rather than overwritten.

## Semantic blocker

The current production DGX profile still aliases `Nvidia P/N` to
`customer_part_number`. This behavior is deliberately unchanged in this fix
pass. It is recorded as:

`KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION`

Until the business owner confirms the relationship among Nvidia P/N,
Customer Part Number, and Our Part Number, evaluation must not report
`customer_part_number` as production-verified. Raw OCR retains the original
`Nvidia P/N` line as evidence. Do not add `nvidia_part_number` or change the
production alias in this checkpoint.

## What this checkpoint does not claim

- no production field accuracy from the unverified seed;
- no camera-wide SLA or production latency SLA;
- no confirmed Nvidia/Customer/Our part-number business mapping;
- no full-dataset benchmark from this fix pass;
- no production monitoring or distributed tracing readiness.
