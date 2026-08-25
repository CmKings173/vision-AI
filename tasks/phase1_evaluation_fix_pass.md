# Phase 1 evaluation fix pass

## Objective

Resolve the previous CRITICAL/HIGH audit findings without changing the
production inference architecture or inventing a business mapping.

## Invariants

- Source data under `phase1_input/phase1_eval_bundle` is read-only.
- `image_id` must match `^[A-Za-z0-9_-]{1,128}$`.
- Every generated artifact must resolve below the current run directory.
- Each run has a unique immutable `<output_root>/<run_id>` directory.
- Production accuracy requires `dataset_role=target`, human verification, and
  a non-synthetic sample.
- Runtime failures stay in the eligible denominator.
- Verified `null` means the reviewer confirmed that the field is absent.
- A predicted PASS is false when any required, semantically unblocked field or
  required DataMatrix is missing or incorrect.
- DataMatrix payload comparison is exact and case-sensitive; format and valid
  state are checked independently.
- Numeric ground-truth weights are canonical kilograms. `KG`/`KGS` are
  kilograms, `G` is divided by 1000, and `LB`/`LBS` is multiplied by
  `0.45359237`.
- `min_condition_samples` is evaluation configuration; its default is 5.

## Current semantic decision

Production behavior is intentionally preserved:

```text
Nvidia P/N -> customer_part_number
```

This mapping is not business-confirmed. The DGX extractor declares a
`KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION`; evaluation suppresses
production verification for `customer_part_number` while preserving observed
counts and raw OCR evidence. The extractor profile version and mapping summary
are written to run provenance.

## Evaluation outputs

The fix pass reports strict field/DataMatrix metrics, real-sample condition
rows for lighting, glare, blur, distance, position, rotation, and occlusion,
and traceable failure rows containing expected/predicted values, confidence,
OCR, barcode, quality, ROI, validator decision, and artifact paths.

Condition rows include sample count, eligible count, accuracy, review rate,
false-PASS count, latency, and an insufficient-sample marker. A metric can be
technically computed while still being marked insufficient; it must not be
presented as a production acceptance claim.

## Verification boundary

This document records implementation semantics, not measured production
performance. GX10 one-image smoke, human verification, and the first real
accuracy run occur only after the fix pass and blocker re-audit succeed.
