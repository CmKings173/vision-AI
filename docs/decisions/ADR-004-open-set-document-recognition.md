# ADR-004: Open-set document recognition and schema discovery

## Status

Accepted for the Phase 1 fail-closed safety boundary. Later phases remain
proposed: no document-recognition model, approved-profile registry, automatic
profile selection, or multi-document business policy is accepted by this ADR.

## Date

2026-09-04

## Scope

This ADR defines the recognition and evidence policy for the current Vision
document-inspection project when the complete set of customer documents is not
known in advance.

It covers:

- document localization and recognition;
- known, unknown, and ambiguous document handling;
- generic evidence extraction;
- semantic mapping provenance;
- extensible canonical fields;
- profile-specific validation;
- incremental discovery of new document families and variants.

It does not decide:

- recognition-model implementation, deployment, or model selection;
- PostgreSQL migrations or the final database schema;
- JWT, authentication, or users;
- ERP integration or `erp_deliveries`;
- creation of additional microservices;
- the business meaning of any unconfirmed field;
- the storage technology used for the profile registry.

## Context

The current system cannot know in advance:

- which customer will provide a document;
- which document or label layout will appear;
- which fields a document contains;
- which source label names are used for those fields;
- which document family or variant will appear;
- which canonical business field, if any, a source label maps to.

The current proof of concept is intentionally narrower. The manual RTSP flow
uses a calibrated detector option, OCR, barcode decoding, and the
`dgx_spark_label` extraction configuration. That controlled behavior is useful
for a known station, but a global list of required fields is not a safe model
for every customer document.

The current closed assumptions create these risks:

1. A document can be valid while not containing fields from another document
   variant.
2. OCR may observe a source label without knowing its business meaning.
3. An unseen document may be incorrectly forced into the closest known type.
4. A missing field can mean `NOT_APPLICABLE`, OCR failure, or an unsupported
   variant; these are not the same condition.
5. Recognition confidence can be mistaken for business validation confidence.

The system therefore needs open-set/open-world behavior. It must be able to
ingest and preserve evidence from a new document without requiring an approved
business profile first.

## Decision

The system SHOULD separate document ingestion, evidence extraction, document
recognition, semantic mapping, and business validation.

The processing policy is:

~~~text
ANY DOCUMENT
    -> document localization
    -> quality and orientation
    -> generic evidence extraction
    -> document recognition
       -> KNOWN + approved profile
          -> semantic mapping + profile validation
          -> PASS / REVIEW / FAIL
       -> UNKNOWN, AMBIGUOUS, or no approved profile
          -> preserve evidence
          -> REVIEW
~~~

A document profile is not required for document ingestion or generic evidence
extraction. Profiles are only required for approved semantic mapping and
profile-specific business validation.

More specifically, a profile is not a prerequisite for:

- ingestion;
- document localization;
- OCR;
- barcode decoding;
- raw evidence extraction;
- evidence persistence.

A profile MUST be required only for:

- approved semantic mappings;
- required, optional, and not-applicable field policy;
- format rules;
- barcode policy;
- automated business validation.

Automated `PASS` MUST only be produced after a known document has been matched
to an approved profile and all required validation rules for that profile have
been evaluated successfully.

Consequently:

- `UNKNOWN`, `AMBIGUOUS`, no approved profile, or validation that has not run
  MUST NOT produce `PASS`; in the absence of a technical failure, they result
  in `REVIEW`;
- uncertainty or insufficient evidence results in `REVIEW`;
- a technical runtime failure results in `ERROR`, while a confirmed business
  rule violation supported by sufficiently reliable evidence results in
  `FAIL`.

## Document Profile definition and boundary

### Definition

**Document Profile** means:

> A versioned, business-approved policy describing how a recognized document
> family/variant should be semantically mapped and validated.

A profile MAY define:

- `family` and `variant` identity;
- a stable `profile_code`;
- `profile_version`;
- approval state;
- required, optional, and not-applicable canonical fields;
- approved source-label to canonical-field mappings;
- barcode policy;
- format rules;
- validation rules.

### What a profile is not

A Document Profile is not:

- an OCR model;
- a document detector;
- a document classifier model;
- a raw OCR schema;
- a list of every field that could ever exist;
- a prerequisite for ingesting a document;
- a global schema applied to every customer;
- proof that a source label has a particular business meaning.

Document evidence MUST remain representable even when no profile exists.

## Localization and recognition

Localization and recognition are separate dimensions.

### Localization status

Localization answers: “How many usable document regions were found in this
frame?”

The allowed localization statuses are:

- `NO_DOCUMENT`;
- `SINGLE_DOCUMENT`;
- `MULTIPLE_DOCUMENTS`.

The locator SHOULD return document regions as a collection, even when the
current station usually contains one document. The architecture MUST NOT
assume that one frame contains exactly one document.

### Recognition status

Recognition answers: “Does the observed document match an approved profile?”

The allowed recognition statuses are:

- `KNOWN`;
- `UNKNOWN`;
- `AMBIGUOUS`.

Recognition reasons are separate from recognition status. The supported reason
vocabulary includes:

- `LOW_CONFIDENCE`;
- `NO_PROFILE_ABOVE_THRESHOLD`;
- `MULTIPLE_CLOSE_CANDIDATES`;
- `INSUFFICIENT_EVIDENCE`;
- `UNSUPPORTED_VARIANT`.

`LOW_CONFIDENCE` is not a recognition status. It is a reason or condition that
may result in `UNKNOWN` or `AMBIGUOUS`.

The contract SHOULD distinguish recognition confidence from business
confidence. A field such as `recognition_confidence` describes the evidence
for matching a document to a profile. It MUST NOT be presented as proof that
the extracted business values are correct.

An implementation MUST NOT use a forced argmax as the final document identity.
If the best candidate does not satisfy the acceptance threshold, the result
remains `UNKNOWN` or `AMBIGUOUS` with a reason.

### Recognition contract

The conceptual result is:

~~~json
{
  "localization": {
    "status": "SINGLE_DOCUMENT",
    "documents": [
      {
        "index": 0,
        "bbox": [120, 80, 980, 720]
      }
    ]
  },
  "recognition": {
    "status": "UNKNOWN",
    "reason": "LOW_CONFIDENCE",
    "recognition_confidence": 0.54,
    "best_candidate": "customer_a"
  }
}
~~~

`NO_DOCUMENT` and `MULTIPLE_DOCUMENTS` belong to localization. They MUST NOT
be encoded as recognition reasons or as a single overloaded document status.
The station or business policy may later decide whether multiple documents are
processed independently, held, reviewed, or rejected; this ADR does not make
that decision.

## Evidence, semantic mapping, and validation

These are three different status dimensions and MUST NOT be represented as one
overloaded field status.

### Evidence/extraction status

Evidence describes what OCR, barcode, layout, or image analysis observed:

- `FOUND`;
- `NOT_FOUND`;
- `AMBIGUOUS`;
- `CONFLICT`;
- `LOW_CONFIDENCE`.

`NOT_FOUND` means that the requested observation was not found. It does not by
itself prove that the field is required or applicable.

### Semantic mapping status

Semantic mapping describes whether an observed source label/value has approved
business meaning:

- `MAPPED`;
- `UNMAPPED`;
- `NOT_APPLICABLE`;
- `AMBIGUOUS_MAPPING`.

`NOT_APPLICABLE` is a policy or semantic decision. It MUST NOT be inferred
merely because OCR did not find a value.

### Validation status

Validation describes the result of applying an approved profile policy. Its
vocabulary MAY include:

- `VALID`;
- `INVALID_FORMAT`;
- `MISSING_REQUIRED`;
- `CONFLICT`;
- `LOW_CONFIDENCE`;
- `UNMAPPED_REQUIRED`.

The outer inspection result MAY still use the existing project-level outcomes
`PASS`, `REVIEW`, `FAIL`, and `ERROR`, but their reasons MUST identify whether
the cause is technical, recognition-related, semantic, or business-policy
validation.

### Example

If OCR observes:

~~~text
NVIDIA P/N: ABC-001
~~~

the evidence and semantic result SHOULD be represented as:

~~~json
{
  "source_label": "NVIDIA P/N",
  "raw_value": "ABC-001",
  "extraction": {
    "status": "FOUND",
    "confidence": 0.99
  },
  "semantic": {
    "status": "UNMAPPED",
    "canonical_field": null,
    "reason": "NEEDS_BUSINESS_CONFIRMATION"
  }
}
~~~

High OCR confidence MUST NOT be interpreted as proof of semantic mapping.

## Extensible canonical vocabulary

The canonical field vocabulary is extensible and MUST NOT be treated as a
closed set known completely in advance.

If OCR observes:

~~~text
CUSTOM REF: ZX-001
~~~

and business has not defined its meaning, the system MUST preserve it as an
unmapped observation:

~~~json
{
  "source_label": "CUSTOM REF",
  "raw_value": "ZX-001",
  "semantic": {
    "status": "UNMAPPED",
    "canonical_field": null
  }
}
~~~

The system MUST NOT:

- drop the observation;
- assign it to a merely similar field;
- invent a canonical meaning.

After business approval, a mapping such as:

~~~text
CUSTOM REF -> customer_reference
~~~

MAY be added as a new version of the approved vocabulary/profile. The original
source label and raw value MUST remain available for provenance.

## Semantic mapping guardrail

The mapping below remains explicitly unconfirmed:

~~~text
NVIDIA P/N -> customer_part_number
~~~

It MUST NOT be treated as production-verified until business confirms the
relationship. Until then, the result SHOULD retain:

~~~text
source_label = NVIDIA P/N
canonical_field = null
semantic.status = UNMAPPED
semantic.reason = NEEDS_BUSINESS_CONFIRMATION
~~~

This ADR does not decide whether the mapping is correct.

## Profile identity and version

The profile identity MUST NOT encode the version twice.

This ADR uses the simple logical pattern:

~~~text
family         = customer_label
variant        = customer_a
profile_code   = customer_a
profile_version = 1
~~~

The profile code MUST NOT be `customer_a_v1` while `profile_version` is also
`1`. The storage and transport representation of this logical identity is
deferred to a later schema/interface decision.

## DocumentProfileRegistry responsibility

`DocumentProfileRegistry` is a logical responsibility, not a storage decision.

It is responsible for resolving approved profiles and their versions. This ADR
does not require the registry to be stored in:

- JSON or YAML in Git;
- PostgreSQL;
- a configuration service;
- any other particular storage technology.

The registry MUST expose only approved profiles to automated business
validation. Draft or unapproved profiles MAY support review and analysis but
MUST NOT produce automated `PASS`.

## Incremental discovery rollout

Open-set behavior MUST be deliverable incrementally. Clustering, vector search,
and automatic profile generation are not prerequisites for the first phase.

### Phase 1: Generic evidence and safe unknown handling

The system SHOULD:

- locate document regions according to the station capability;
- run quality/orientation and generic evidence extraction;
- preserve raw OCR, barcode, layout, and image evidence;
- return `UNKNOWN` or `AMBIGUOUS` when recognition is not established;
- return `REVIEW` for unknown or ambiguous recognition;
- avoid semantic guessing.

### Phase 2: Approved profile recognition

The system MAY add:

- retrieval against approved profiles;
- known/unknown/ambiguous threshold evaluation;
- profile-specific semantic mappings;
- profile-specific required/optional/not-applicable policy;
- profile-specific validation.

### Phase 3: Discovery assistance

The system MAY later add:

- document fingerprints;
- similarity search;
- clustering of unknown documents;
- human-assisted profile drafting and promotion.

Phase 3 improves catalog maintenance. It is not required to establish the
open-set safety behavior in Phase 1.

## Evidence namespace

Evidence for a localized document SHOULD be addressable independently from the
inspection and from other documents in the same frame.

The conceptual namespace is:

~~~text
inspections/<inspection_id>/documents/<document_index>/
~~~

An inspection MAY also contain frame-level evidence alongside the document
regions:

~~~text
inspections/<inspection_id>/
    original_frame
    documents/
        0/
            document_crop
            ocr_evidence
            barcode_evidence
            layout_evidence
            recognition_evidence
~~~

These are logical artifact categories, not a commitment to exact filenames or
to a particular object store. If discovery later becomes an independent
aggregate, it MAY introduce a separate `discoveries/<discovery_id>/` namespace
through a future ADR.

## Consequences

### Positive consequences

- New document types can be ingested without a pre-existing profile.
- Unknown and ambiguous documents become visible instead of silently being
  misclassified.
- Raw evidence remains available when semantic meaning is unresolved.
- Canonical fields can evolve without pretending the initial vocabulary is
  complete.
- Required fields and validation policy can vary by approved document variant.
- Recognition, semantic mapping, and business validation become explainable
  separately.

### Costs and risks

- Unknown documents need a review and catalog-maintenance process.
- An approved profile registry and business approval workflow are required
  before automated validation can cover a new variant.
- Document localization beyond a controlled ROI may require additional model
  evaluation later.
- Thresholds require representative evidence and must be evaluated separately
  from business validation accuracy.
- No automated `PASS` is available for an unapproved or ambiguous document.

## Alternatives considered

### Global fixed field schema

Rejected as the general policy because it treats every customer document as the
same schema and cannot distinguish `NOT_APPLICABLE` from `NOT_FOUND`.

The current DGX Spark configuration MAY remain as a controlled station/profile
scope while the open-set capability is developed.

### Closed-set document classifier

Rejected because it forces unseen documents into an existing class. A high
classifier score would not establish that the selected business policy is
correct.

### VLM/LLM as the sole interpreter

Rejected as the sole production authority because semantic mappings and
validation outcomes would be difficult to bound and audit. A VLM/LLM MAY be
used later to assist human review or profile drafting, but it MUST NOT be the
only authority for automated business `PASS`.

### Open-set recognition with incremental profiles

Recommended because it preserves unknown evidence, separates technical
recognition from business semantics, and allows the approved document catalog
to grow over time without forced classification.

## Open decisions for business discussion

The following decisions remain intentionally open:

1. Which document families are in scope for the first business workflow?
2. What are the business meanings of `NVIDIA P/N`, `Customer P/N`, and `Our
   P/N`?
3. Which fields are required, optional, or not applicable for each approved
   document variant?
4. What should the station do when localization finds multiple documents?
5. Should multiple documents be processed independently, held, reviewed, or
   rejected?
6. Which recognition threshold is sufficient to classify a document as
   `KNOWN`?
7. Which additional business-specific conditions, beyond the minimum automated
   `PASS` invariant in this ADR, permit `PASS` versus `REVIEW`?
8. How long should unknown-document evidence be retained?

## Guardrails

Until the relevant business mappings and per-profile validation policies are
approved:

- unknown documents MUST NOT be forced into a known profile;
- unknown and ambiguous recognition MUST result in `REVIEW`;
- an unapproved profile MUST NOT produce automated `PASS`;
- `NVIDIA P/N` MUST NOT be mapped to `customer_part_number` as a production
  fact;
- raw observations MUST be preserved when semantic mapping is unresolved;
- the canonical vocabulary MUST remain extensible;
- Fixed ROI MAY remain a station-specific optimization, but MUST NOT be the
  only architectural assumption;
- VLM/LLM output MUST NOT be the sole production authority;
- technical `ERROR` MUST remain distinct from business `FAIL`;
- required fields MUST come from the approved document profile, not a global
  list applied to every customer document;
- recognition confidence MUST remain distinct from business confidence.

## Current project impact

The Phase 1 safety subset is implemented in the current runtime:

- OCR, barcode decoding, and generic evidence collection run without a
  profile;
- profile-free, unapproved, unknown, and ambiguous paths persist evidence,
  emit no canonical `extracted` fields, and return `REVIEW` after successful
  technical processing;
- the processor and the durable worker boundary independently require an
  approved `ProfileBinding` plus a matching `DocumentRecognitionResult` with
  status `KNOWN` before allowing `PASS` or `FAIL`;
- the durable boundary does not publish canonical fields or business reasons
  returned by an unauthorized semantic path; it preserves the underlying raw
  evidence and publishes only its own profile/recognition review reason;
- the worker persists the complete recognition state, reason, and profile
  binding in runtime provenance, while retaining the derived legacy boolean;
- the worker re-derives its runtime descriptor before each new inference and
  persists `WORKER_RUNTIME_DRIFT` as a technical `ERROR` if startup identity
  has changed;
- process-wide required-field and barcode settings do not become policy for an
  approved profile. The current factory fails startup for such a profile until
  a profile-owned validation-policy boundary is implemented.

The current factory supplies no trusted recognition result and no approved
profile. The DGX Spark patterns therefore remain draft analysis definitions;
they can support evidence evaluation but cannot emit production canonical
fields or authorize a business decision.

This implementation does not change OCR/barcode threading, camera acquisition,
FixedROI behavior, Local Spool behavior, database schema, authentication, or
ERP integration. Recognition models, an approved-profile registry, discovery
ML, and multi-document business policy remain future work.
