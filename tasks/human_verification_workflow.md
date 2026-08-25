# Phase 1 Human Verification Workflow

Current state: `NEEDS_HUMAN_VERIFICATION`.

The current real DGX image is suitable for runtime smoke testing, but its
ground truth is intentionally blank. Do not fill ground truth from OCR,
barcode output, or a generated synthetic variant.

## Verify one image

For each real camera image, a reviewer must inspect the saved image and record:

- NVIDIA P/N, if present;
- Customer Part Number;
- Our Part Number;
- S/O Number;
- Quantity;
- Net Weight and Gross Weight, including units;
- Carton Number and any separate Carton ID;
- DataMatrix payload;
- expected business status (`PASS`, `REVIEW`, `FAIL`, or `ERROR`);
- lighting, glare, blur, occlusion, rotation, distance, and position;
- reviewer identity, timestamp, and notes.

NVIDIA P/N, Customer Part Number, and Our Part Number must be reviewed as
potentially separate business concepts. The current production extractor still
aliases NVIDIA P/N to `customer_part_number` for backward compatibility; that
mapping is a `KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION` and must
not be used to verify customer-part accuracy. Do not add `nvidia_part_number`
or alter the production mapping until business confirmation is recorded in a
separate change.

## Add a verified record

1. Copy the original real camera image into the target dataset `images/`.
2. Add one matching ground-truth record and manifest row.
3. Fill every expected business field and condition explicitly.
4. Set `human_verified=true` only after review; record reviewer metadata in the
   worksheet or an approved audit store.
5. Run `validate_eval_dataset.py` and require zero errors.
6. Run the evaluator and inspect the raw OCR, barcode, crop, quality, and
   prediction artifacts before accepting metrics.

Synthetic and public images may be used for robustness reports, but they never
enter DGX production accuracy. A verified production result requires a real
target image and verified business ground truth.
