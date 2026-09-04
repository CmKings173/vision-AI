# Open-Set Evidence-Only Runtime Design

## Status

Approved for implementation by the current task request.

## Goal

Make the runtime fail closed for every profile that is not explicitly approved
for automated business interpretation. Unknown and unapproved documents must
still be ingestible, OCR/barcode evidence must be preserved, and the final
business outcome must be `REVIEW` unless a technical failure produces `ERROR`.

## Context

The current implementation has three safety gaps:

- profile approval is inferred from the absence of semantic blockers;
- extractor and validator can carry different profile identities;
- an unapproved profile still publishes canonical fields before validation
  forces the outer result to `REVIEW`.

The current DGX Spark mapping of `NVIDIA P/N` to
`customer_part_number` is not business-confirmed. It must remain available for
future analysis but cannot be used as a production semantic mapping.

`requested_profile` is only a station hint. It is not document recognition.
Semantic extraction and automated `PASS` also require an explicit,
profile-bound `DocumentRecognitionResult` with status `KNOWN`; the current
production factory supplies no such result.

## Decision

### Explicit profile binding

Introduce one immutable `ProfileBinding` shared by extractor, validator,
processor, and worker provenance. It contains:

- `name: str | None`;
- `version: str | None`;
- `approval_status`, with exactly `UNAPPROVED` or
  `APPROVED_FOR_AUTOMATED_PASS`.

An unprofiled binding has null name/version and `UNAPPROVED` status. A named
profile is never considered approved merely because its blocker list is empty.
The binding rejects partial identity and rejects approval without a named
profile.

### Evidence-only behavior

The processor always runs localization/preparation, OCR, barcode decoding and
generic evidence collection. It runs semantic extraction and profile-specific
validation rules only when the shared binding is explicitly approved and a
matching document-recognition result has status `KNOWN`.

For profile-free, named-but-unapproved, or not-yet-recognized processing:

- raw OCR, barcode, image and quality evidence remain available;
- `extracted` is `{}`;
- profile required fields and profile barcode policy are not applied;
- the validator returns `REVIEW` with `NO_APPROVED_PROFILE` for a profile-free
  or unapproved binding, or `NO_TRUSTED_DOCUMENT_RECOGNITION` for an approved
  binding that is not recognized as `KNOWN`;
- OCR, barcode, quality or other technical failures remain `ERROR`.

The existing DGX patterns remain draft research definitions. The runtime marks
the DGX binding `UNAPPROVED`, so no DGX canonical field is emitted.

### Compatibility contract

Worker provenance uses the shared binding as the compatibility source, not the
extractor alone. Extractor and validator identity/version/approval must match
at processor construction. The worker pipeline provenance version is bumped
to `phase2-worker.v2` because the runtime descriptor and profile semantics are
changed. The station continues to emit `requested_profile: null` for
profile-free operation and emits the exact named profile identity for named
operation.

Profile names use normalized comparison. Profile versions are opaque identity
strings: surrounding whitespace is removed, but punctuation and case are
preserved exactly for provenance and compatibility checks.

Worker runtime provenance persists the complete document-recognition status,
reason, and profile binding. The derived `trusted_document_recognition`
boolean remains only for backward-compatible readers.

Existing legacy `default` requests may be accepted by a v2 profile-free worker
as a safe migration alias that results in evidence-only `REVIEW`; v2 station
jobs must not be routed to pre-v2 workers. Deployment ordering and queue drain
are documented as an operational requirement rather than silently pretending
that old workers understand the new null contract.

### Evidence snapshot integrity

Evidence items snapshot polygon and metadata values at collection time using
immutable JSON-compatible containers. Serializing an evidence item must
produce the same logical JSON after the original OCR/barcode objects are
mutated.

### Durable worker safety boundary

The processor gate is not the final authority. Before persistence, the worker
must independently clear canonical fields and force `REVIEW` for any
`PASS`/`FAIL` result whose startup descriptor does not contain both an approved
profile and matching `KNOWN` recognition. It re-derives the runtime descriptor
before every new inference; drift produces durable technical
`WORKER_RUNTIME_DRIFT` without running inference.

Validation reasons emitted by an unauthorized semantic path are not trusted
business facts. The durable result keeps raw evidence and quality observations
but replaces those reasons with the worker-owned profile/recognition gate
reason.

Process-wide required-field and barcode settings are not approved-profile
policy. Until a profile-owned validation-policy resolver exists, the factory
must fail startup for a profile marked `APPROVED_FOR_AUTOMATED_PASS`.

## Outcome policy

| Condition | Business outcome |
|---|---|
| Technical OCR/barcode/quality/runtime failure | `ERROR` |
| No profile, unknown profile, or unapproved profile | `REVIEW` |
| Approved profile with insufficient evidence | `REVIEW` |
| Approved profile with confirmed business violation | `FAIL` |
| Approved profile with matching `KNOWN` document recognition and all configured rules valid | `PASS` |

OCR confidence remains an observation confidence. It cannot authorize a
semantic mapping or business `PASS`.

## Testing requirements

The implementation must add tests for:

- explicit profile approval, including a named profile with no blockers still
  being unapproved by default;
- processor rejection of extractor/validator binding mismatch;
- profile-free and named-unapproved evidence-only processing;
- no canonical DGX fields for the unapproved DGX profile;
- technical failures remaining `ERROR`;
- profile-free station-to-worker provenance with `requested_profile: null`;
- legacy `default` compatibility being safe and ending in `REVIEW`;
- evidence immutability after source-object mutation;
- v2 durable result provenance identity, including full recognition state;
- worker-boundary rejection of unauthorized `PASS`, `FAIL`, and canonical
  fields;
- runtime-descriptor drift before inference;
- successful `PASS` retaining an empty reason list;
- profile version punctuation surviving provenance compatibility checks;
- approved profiles not inheriting global validation settings.

## Non-goals

This change does not add database tables, authentication, ERP integration,
automatic document clustering, multi-document business policy, or a new OCR,
barcode, or detection model.
