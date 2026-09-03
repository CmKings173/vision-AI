# ADR-004: Open-set document recognition and schema discovery

## Status

Proposed. This document is a discussion baseline and is not approval to
implement or deploy the proposed recognition policy.

## Date

2026-09-03

## Scope

This ADR defines a direction for recognizing customer labels and other
documents when the complete set of document types is not known in advance.

It covers:

- document localization and recognition;
- known, unknown, and ambiguous document handling;
- discovery of new document families and variants;
- OCR evidence and field extraction contracts;
- semantic mapping provenance;
- validation behavior for known and unknown documents.

It does not decide:

- JWT or authentication;
- ERP delivery or `erp_deliveries`;
- the final PostgreSQL migration;
- the final business meaning of `NVIDIA P/N`;
- the exact machine-learning model for every recognition stage.

## Context

The current inspection pipeline is designed around a known DGX Spark label
profile. The manual RTSP script configures a fixed ROI and a fixed list of
required fields:

~~~text
Fixed ROI
    -> OCR
    -> barcode
    -> fixed DGX Spark fields
    -> validation
~~~

This is reasonable for a controlled proof of concept where the camera view,
label layout, and business fields are already known. It is not sufficient for
the wider requirement of accepting customer labels whose number, layout, and
field set are not yet known.

The current design has several risks:

1. A single global required-field list treats every label as if it has the
   same schema.
2. The extractor represents an absent field as `NOT_FOUND`, although the
   field may be genuinely not applicable to that document.
3. The profile currently aliases `NVIDIA P/N` to `customer_part_number`.
   Business has not confirmed that semantic mapping.
4. A closed-set classifier could force an unseen document into the closest
   known type instead of identifying it as new.
5. Validation cannot determine whether a missing value means an OCR failure,
   an unsupported document variant, or a field that does not apply.

The system therefore needs an open-set or open-world behavior: it must be
able to process evidence from a new document without pretending that the
document belongs to a known business schema.

## Decision proposal

Adopt an open-set document-recognition architecture with explicit discovery
and profile promotion.

The system should recognize a document in two separate dimensions:

1. **Document identity:** what family or variant the document appears to be.
2. **Document content:** what text, barcode, layout elements, and candidate
   fields were actually observed.

Business validation is only applied when document identity is known with
adequate confidence and a profile exists for that identity.

An unseen or ambiguous document must be preserved as evidence and returned as
`REVIEW`, rather than being forced into an existing profile.

## Proposed processing flow

~~~text
Camera / input frame
    |
    v
Document localization
    |  Find one or more document regions; do not require one fixed ROI only
    v
Quality and orientation
    |
    v
Evidence extraction
    |  OCR tokens + boxes, barcode, layout, image features
    v
Document recognition
    |
    +--> KNOWN profile/variant
    |       |
    |       v
    |   Profile-specific extraction and validation
    |
    +--> UNKNOWN document
    |       |
    |       v
    |   Persist evidence and return REVIEW
    |
    +--> AMBIGUOUS match
            |
            v
        Persist evidence and return REVIEW
~~~

The current fixed-ROI detector can remain as a station-specific optimization,
but it must become one detector option rather than the only mechanism for
finding documents.

## Document recognition model

The initial recognition contract should distinguish a document family from a
specific layout variant:

~~~json
{
  "document": {
    "status": "KNOWN",
    "family": "customer_label",
    "variant": "customer_a_v1",
    "profile_id": "customer_a_v1",
    "confidence": 0.94,
    "match_method": "OCR_LAYOUT_RETRIEVAL",
    "profile_version": "1"
  }
}
~~~

The minimum document statuses are:

- `KNOWN`: one approved profile matches above its acceptance threshold;
- `UNKNOWN`: no approved profile matches sufficiently;
- `AMBIGUOUS`: multiple profiles are plausible or confidence is too close;
- `LOW_CONFIDENCE`: a candidate exists but evidence is insufficient;
- `NO_DOCUMENT`: no usable document region was found;
- `MULTIPLE_DOCUMENTS`: more than one document was found and the station
  policy does not define how to process them.

The recognizer must not use a forced argmax result as the final identity. A
low-confidence best match is still `UNKNOWN` or `AMBIGUOUS` when it does not
meet the profile acceptance policy.

## Evidence extraction contract

Evidence extraction must be non-destructive. It records what the system saw
before assigning business meaning.

The proposed output has two layers:

1. `observations`: raw or normalized observations from OCR, barcode, and
   layout analysis;
2. `fields`: canonical business fields only when an approved mapping exists.

Example:

~~~json
{
  "observations": [
    {
      "source_label": "NVIDIA P/N",
      "raw_value": "ABC-001",
      "normalized_value": "ABC-001",
      "source_line": "NVIDIA P/N: ABC-001",
      "confidence": 0.96,
      "semantic_status": "UNMAPPED",
      "canonical_field": null
    },
    {
      "source_label": "QTY",
      "raw_value": "10",
      "normalized_value": "10",
      "source_line": "QTY: 10",
      "confidence": 0.94,
      "semantic_status": "MAPPED",
      "canonical_field": "quantity"
    }
  ],
  "fields": {
    "quantity": {
      "value": "10",
      "status": "FOUND",
      "confidence": 0.94,
      "provenance": "approved_profile_mapping"
    }
  }
}
~~~

The evidence contract should preserve, where available:

- source label text;
- raw value and normalized value;
- OCR source line and token/region coordinates;
- confidence;
- document region and frame identity;
- barcode format, value, and decode status;
- extraction method;
- mapping rule and mapping version;
- conflicts and alternative candidates.

The extractor must not drop a value merely because it cannot map that value to
a canonical field.

## Field statuses

The following statuses separate absence, uncertainty, and semantic decisions:

| Status | Meaning | Default validation effect |
|---|---|---|
| `FOUND` | A field/value was identified with usable evidence | Continue validation |
| `NOT_FOUND` | The active profile expects the field but no value was found | `REVIEW` |
| `NOT_APPLICABLE` | The active profile says the field does not apply | No missing-field failure |
| `AMBIGUOUS` | Multiple candidates cannot be selected safely | `REVIEW` |
| `CONFLICT` | Evidence contains contradictory values | `REVIEW` |
| `UNMAPPED` | Source label/value is known to OCR but has no approved canonical mapping | `REVIEW` when business-critical |
| `LOW_CONFIDENCE` | A candidate exists below the acceptance threshold | `REVIEW` |
| `INVALID_FORMAT` | Value is present but violates the active profile format rule | `FAIL` or `REVIEW` by policy |

`NOT_APPLICABLE` must not be inferred merely because OCR did not find a field.
It should come from the recognized document profile or an explicit business
policy.

## Semantic mapping policy

No unconfirmed semantic mapping is production-approved.

In particular, the current mapping below remains a known blocker:

~~~text
NVIDIA P/N -> customer_part_number
~~~

Until business confirms this relationship, the safe behavior is:

~~~text
source_label = NVIDIA P/N
canonical_field = null
semantic_status = UNMAPPED
provenance = NEEDS_BUSINESS_CONFIRMATION
~~~

An approved mapping registry should eventually contain:

~~~json
{
  "source_label": "CUSTOMER P/N",
  "canonical_field": "customer_part_number",
  "approved_by": "business_owner",
  "mapping_version": "1",
  "effective_at": "2026-09-03T00:00:00Z"
}
~~~

Raw observations must remain available even after a mapping is approved. This
allows the system to explain how a canonical value was produced and to revise
the mapping without losing the original evidence.

## Profile and variant model

Each approved document variant should define its own policy rather than
sharing one global required-field list.

~~~json
{
  "profile_id": "customer_a_v1",
  "family": "customer_label",
  "version": "1",
  "required_fields": ["quantity"],
  "optional_fields": ["lot_number", "net_weight"],
  "not_applicable_fields": ["gross_weight"],
  "barcode_policy": "OPTIONAL",
  "format_rules": {
    "quantity": "positive_integer"
  }
}
~~~

The profile registry is the source of policy for both extraction and
validation. The extractor and validator must not independently maintain
incompatible field lists or format rules.

## Validation policy

Validation should operate in this order:

1. Check that a usable document region exists.
2. Check quality and OCR/runtime health.
3. Resolve document family and variant.
4. If the profile is unknown or ambiguous, return `REVIEW` with the document
   recognition reason.
5. For a known profile, validate only its required fields.
6. Treat optional and not-applicable fields according to profile policy.
7. Treat ambiguous, conflicting, low-confidence, and unapproved semantic
   mappings as `REVIEW`.
8. Apply format and barcode policy from the same profile.

Suggested status semantics:

~~~text
Technical runtime failure       -> ERROR
Unknown document type            -> REVIEW
Ambiguous document type          -> REVIEW
Missing required field           -> REVIEW
Not-applicable optional field    -> no failure
Unmapped business-critical field -> REVIEW
Clearly invalid format           -> FAIL or REVIEW by profile policy
All required evidence valid      -> PASS
~~~

The validator should not directly infer business meaning from a source label
or from a high OCR confidence. OCR confidence says how likely the text is
correct; it does not prove that the text has a particular business meaning.

## Discovery mode and profile promotion

Because the full document catalog is not known, the system needs a discovery
workflow before enforcing production validation for every document.

### Discovery capture

For each document event, persist enough evidence to reproduce classification:

~~~text
discovery/<event_id>/
    original_frame.jpg
    document_crop.jpg
    ocr.json
    barcode.json
    layout.json
    fingerprint.json
~~~

The existing Local Spool can provide durable local storage for this evidence
when a station must survive network or service interruptions. It is not a
replacement for a document registry or a business review tool.

### Clustering and review

Similar documents can be grouped using OCR, layout, and visual features:

~~~text
new evidence
    -> fingerprint
    -> nearest known profile or cluster
    -> known / ambiguous / unknown
    -> human review for new clusters
~~~

Human review assigns a business family, identifies variants, and defines the
required/optional/not-applicable fields. Only then is a cluster promoted to an
approved profile.

### Profile lifecycle

~~~text
DISCOVERED
    -> REVIEWED
    -> PROFILE_DRAFT
    -> BUSINESS_APPROVED
    -> ACTIVE
~~~

An unapproved draft must not be used to produce an automated `PASS` result.

## Proposed module boundaries

The future implementation should separate these responsibilities:

~~~text
DocumentLocator
    Finds document regions in a frame.

DocumentRecognizer
    Resolves known/unknown/ambiguous identity.

DocumentProfileRegistry
    Stores approved profiles and their versions.

EvidenceExtractor
    Produces OCR/barcode/layout observations without semantic guessing.

SemanticMapper
    Applies only approved, versioned mappings.

PolicyValidator
    Validates a mapped result against the selected profile.

DiscoveryStore
    Preserves unknown evidence for clustering and human review.
~~~

Each boundary should have a stable input/output contract. Recognition output
must not expose an implementation-specific classifier score as if it were a
business confidence score; the contract should state what the score means and
which threshold produced the status.

## Alternatives considered

### Fixed global schema

Keep one `DGX_SPARK_LABEL_FIELDS` list for every label.

Rejected as the general solution because it marks genuinely absent fields as
missing and cannot represent label variants safely. It can remain as a
temporary profile for one controlled station while discovery is developed.

### Closed-set classifier

Train a classifier that always chooses one of the currently known document
types.

Rejected because an unseen document would be forced into an existing type. The
result can look confident while applying the wrong extraction and validation
policy.

### VLM/LLM as the sole document interpreter

Send every image to a multimodal model and use its answer as the final
document type and field mapping.

Rejected as the sole production authority because the output can be
non-deterministic, difficult to validate, and vulnerable to semantic mistakes
such as the unconfirmed `NVIDIA P/N` mapping. It may be useful as a discovery
assistant or review aid.

### Open-set hybrid recognition

Combine document localization, OCR/layout evidence, barcode evidence, visual
similarity, profile retrieval, and explicit unknown thresholds.

Recommended because it supports unknown documents, preserves evidence, allows
incremental catalog growth, and keeps business validation policy explicit.

## Consequences

### Positive consequences

- New customer documents do not have to be known before ingestion.
- Unknown documents are visible instead of silently misclassified.
- Raw OCR evidence is preserved even when semantic mapping is unresolved.
- Required fields can differ by document variant.
- Business mappings can be approved and versioned independently of OCR.
- Validation becomes explainable: identity, evidence, mapping, and policy are
  separate reasons.

### Costs and risks

- A discovery/review process is required.
- A document profile registry must be maintained.
- Full-frame document localization is more complex than one calibrated ROI.
- Unknown documents cannot receive automated `PASS` until a profile exists.
- Clustering and visual retrieval require additional storage and evaluation.
- Recognition thresholds need a representative validation dataset.

## Open decisions for business discussion

The following items must be resolved before implementation of the new policy:

1. Which document families are in scope: product labels, shipping labels,
   packing lists, invoices, or other documents?
2. What is the business meaning of `NVIDIA P/N`, `Customer P/N`, and `Our P/N`?
3. Which fields are required for each approved document family/variant?
4. Is a document with an unknown type allowed to continue physically, or must
   the station stop until human review?
5. Can one camera frame contain multiple documents?
6. Should multiple documents be processed independently or rejected as a
   station condition?
7. What evidence-retention period is needed for unknown documents?
8. Which profile and confidence thresholds are acceptable for automated `PASS`?

## Implementation guardrails

Until this ADR is accepted and the business mappings are confirmed:

- do not treat the global DGX Spark field list as valid for every customer
  document;
- do not map `NVIDIA P/N` to `customer_part_number` as a production fact;
- preserve source labels and raw values;
- return `REVIEW` for unknown or ambiguous document identity;
- do not use an unapproved profile to produce automated `PASS`;
- keep the current fixed-ROI POC behavior explicitly scoped to its controlled
  station configuration.

## Current conclusion

The system should evolve from a closed, fixed-label pipeline into an open-set
document ingestion and recognition pipeline. The first production-safe goal is
not to understand every unseen document automatically. It is to detect and
preserve unseen documents, classify known documents only when evidence is
adequate, and build approved profiles over time without inventing business
semantics.

No runtime code, database schema, authentication flow, or ERP integration is
changed by this ADR.
