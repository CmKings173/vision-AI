# Open-set document recognition — Phase 1 implementation plan

## Goal

Align the current inspection runtime with the safety boundary in ADR-004:

- a document can be ingested and its OCR/barcode evidence preserved without a
  known profile;
- configured profiles are the only source of semantic field extraction and
  profile-specific validation;
- an unprofiled or unapproved semantic path can never produce `PASS`;
- technical `ERROR` remains distinct from business `REVIEW`;
- no business field mapping, document taxonomy, clustering policy, or
  multi-document policy is invented in this change.

## Non-goals

- Do not change the existing NVIDIA P/N mapping.
- Do not select or implement a document-classification ML model.
- Do not decide how multiple documents in one frame are handled.
- Do not add database tables, JWT/authentication, ERP integration, or a
  persistence implementation for a profile registry.

## Design

### 1. Generic evidence boundary

Add a profile-independent `EvidenceItem` representation to the local result.
The processor will emit one item for every OCR line and every barcode result,
including source, confidence, polygon, and barcode metadata where available.
This is an evidence inventory only; it will not infer canonical field names.
The existing raw OCR and barcode payloads remain unchanged.

### 2. Explicit unprofiled mode

Make the default extractor profile-free (`fields=()`), while retaining the
named DGX Spark extractor as an explicit profile. `default`, `none`, and
`unprofiled` configuration values will resolve to the same profile-free mode.
Profile-free extraction returns no semantic fields and does not fail ingestion.

### 3. Fail-closed semantic validation

Add profile identity and approval state to the validator configuration. A
profile-free or unapproved processor may still return technical `ERROR` for a
failed OCR/quality stage, but otherwise its business status is forced to
`REVIEW` with an explicit no-approved-profile reason. Existing configured
profile behavior remains available and retains the current field rules.

### 4. Distributed provenance compatibility

Represent an unprofiled request as `requested_profile: null` in station
provenance. Worker compatibility checks will accept null only when the worker
is also profile-free, and continue requiring exact name/version equality for a
named profile. This keeps profile identity a runtime contract without
implying a storage technology.

## Implementation steps

1. Add `EvidenceItem` and evidence collection from raw OCR/barcode results.
2. Add evidence to `InspectionResult`, local JSON payloads, and worker result
   payloads through the existing `to_dict()` path.
3. Add explicit profile-free extractor construction and update processor/app
   wiring so `default` does not create the legacy SKU/LOT semantic set.
4. Add the fail-closed validator gate and expose profile metadata in worker
   provenance.
5. Update station provenance and configuration validation for profile-free
   operation.
6. Add focused tests for unknown/unprofiled processing, evidence preservation,
   PASS prevention, and profile provenance compatibility; run the complete
   regression suite.

## Acceptance criteria

- An OCR result containing arbitrary customer-label text is preserved in
  `result.evidence` and `result.raw_ocr` without a profile.
- Profile-free processing returns `extracted == {}` and `REVIEW`, never
  `PASS` or `FAIL`, when no technical error exists.
- OCR/quality technical failure still returns `ERROR` and is not relabeled as
  a business result.
- The named DGX Spark profile still extracts its configured fields and keeps
its existing semantic-blocker provenance, while remaining ineligible for
automated PASS until those blockers are resolved.
- A profile-free worker accepts `requested_profile: null`; a named worker
  rejects null or mismatched name/version, and named compatibility behavior is
  unchanged.
- Existing tests pass, with no changes to database/auth/ERP/multi-document
  policy.

## Verification

- Focused unit/integration tests for extraction, validator, processor,
  provenance, and result serialization.
- Full `python -m pytest -q` suite.
- Review the final diff to ensure only the scoped files and tests are staged.
