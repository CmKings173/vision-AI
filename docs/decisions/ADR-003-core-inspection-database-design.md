# ADR-003: Core PostgreSQL data model for inspection processing

## Status

Proposed. This document records the review and implementation proposal for the
core inspection data model. It is not an approval to deploy the schema.

## Date

2026-08-28

## Scope

This ADR reviews only the inspection-processing data model described in
D:\Vision-Platform_Database_Design_V1.md:

- inspections
- inspection stages
- inspection_results
- inspection_events
- inspection artifacts stored in MinIO and referenced by PostgreSQL

Authentication/JWT, users, and ERP delivery are intentionally out of scope.

## Context

The proposed design correctly separates structured inspection metadata from
binary image storage and message transport:

~~~text
PostgreSQL  -> inspection state, results, events, artifact metadata
MinIO       -> frames, crops, raw large outputs
RabbitMQ    -> job transport
~~~

The current repository does not yet implement the PostgreSQL side of this
architecture. There is no PostgreSQL driver, migration set, database adapter,
ORM/repository layer, or active Station/Worker database writer. The current
runtime still uses the local spool, MinIO, RabbitMQ, and JSON result artifacts.

The proposed V1 document stores stage_states and artifacts as JSONB inside
inspections. That is simple to start with, but it becomes unsafe when the
Station and Worker update the same inspection concurrently. One update can
overwrite fields written by another process.

## Decision proposal

Use inspections as the inspection-level projection and introduce normalized
tables for independently changing stages and artifacts.

~~~text
inspections
    |
    +-- inspection_stages
    +-- inspection_results
    +-- inspection_events
    +-- inspection_artifacts
~~~

Keep JSONB where the structure is genuinely flexible, such as result payloads,
event-specific metadata, and raw model output references. Do not use a single
mutable JSONB object as the authoritative store for independently owned
pipeline stages.

### 1. inspections

One row represents one inspection trigger.

It should contain the common identity, lifecycle, timing, and dashboard
projection fields:

~~~text
id
inspection_type
workflow_version
processing_status
business_status
triggered_at
completed_at
duration_ms
summary JSONB
created_at
updated_at
~~~

Recommended database rules:

- use timestamptz for all timestamps;
- provide defaults for id, status, and timestamps;
- add CHECK constraints for status values and non-negative durations;
- define valid lifecycle transitions instead of allowing arbitrary status updates;
- add a stable tie-breaker (id) to time-based list indexes;
- treat summary as a denormalized read projection, not the source of truth.

The source of truth for stage detail is inspection_stages.

### 2. inspection_stages

Each stage gets one independently updateable row per inspection:

~~~text
inspection_id
stage_key
status
attempt
started_at
completed_at
duration_ms
error_code
error_message
metadata JSONB
~~~

Recommended primary key:

~~~text
PRIMARY KEY (inspection_id, stage_key)
~~~

Initial stage keys should be explicit:

~~~text
detection
quality
ocr
barcode
validation
~~~

Stage ownership should be explicit:

~~~text
Station -> detection, quality, selected frame, detector artifacts
Worker  -> ocr, barcode, result data, validation
~~~

This ownership model avoids Station and Worker rewriting the same JSONB value.
If a stage can be retried, increment attempt and preserve the attempt timing
and error information according to the result-retention policy.

### 3. inspection_results

Use this table for business output, not for transient stage state:

~~~text
inspection_id
result_type
result_index
schema_version
status
data JSONB
validation JSONB
metrics JSONB
created_at
updated_at
~~~

JSONB is appropriate because shipping-label, barcode, and weight results have
different shapes.

The implementation must choose one result lifecycle before creating the
unique constraint:

1. One current result: unique on
   (inspection_id, result_type, result_index) and retry with an idempotent
   upsert.
2. Multiple model runs: add attempt and model_version, and mark one result as
   current. This is required if OCR or detection can be rerun while retaining
   prior output.

Do not use revision for both database concurrency and model-run history.
They solve different problems.

Large raw OCR/detector output should be stored in MinIO and represented by an
artifact reference, rather than placed directly into a growing JSONB column.

### 4. inspection_events

Events are append-only timeline records:

~~~text
id
inspection_id
sequence_no
event_type
stage
source
status
occurred_at
recorded_at
dedupe_key
data JSONB
created_at
~~~

Use sequence_no for deterministic ordering within an inspection. Timestamps
from different hosts can have clock skew, so occurred_at alone is not a
reliable ordering key.

Retries must not create duplicate logical events. Add a deduplication key and
enforce uniqueness, for example:

~~~text
UNIQUE (inspection_id, dedupe_key)
~~~

The event table should be append-only. inspections and inspection_stages are
projections that can be updated; events preserve what happened.

### 5. inspection_artifacts

Store artifact metadata in a first-class table while keeping binary content in
MinIO:

~~~text
id
inspection_id
artifact_type
ordinal
bucket
object_key
content_type
size_bytes
sha256
created_at
retention_until
~~~

Recommended uniqueness:

~~~text
UNIQUE (inspection_id, artifact_type, ordinal)
~~~

Store an object key, not a long-lived signed URL. Generate a signed URL only
when the API returns an artifact to a client. The checksum and size allow the
database record and MinIO object to be reconciled.

## revision: when it is needed

revision is an optimistic-concurrency counter. It prevents a stale reader
from overwriting a newer update.

Example:

~~~text
Station reads revision 5
Worker reads revision 5
Station writes revision 6
Worker tries to write using revision 5 -> update is rejected
~~~

The guarded update is conceptually:

~~~sql
UPDATE inspections
SET summary = $new_summary,
    revision = revision + 1,
    updated_at = now()
WHERE id = $inspection_id
  AND revision = $expected_revision;
~~~

If the affected-row count is zero, the caller must reread and retry or report
a conflict.

revision is not a workflow version, JSON schema version, migration version, or
model version.

With normalized stages and one owner per stage, a global revision is less
important. It is still useful if several processes update the inspection-level
projection concurrently. If stage_states remains the authoritative JSONB field,
optimistic locking is mandatory.

## Consistency across PostgreSQL, MinIO, and RabbitMQ

These systems do not share one distributed transaction. The implementation
must define recovery for partial failures:

~~~text
DB commit succeeds, Rabbit publish fails
MinIO upload succeeds, DB update fails
Rabbit publish succeeds, Worker crashes before writing the result
~~~

The minimum required behavior is:

1. create the inspection and its initial state;
2. make the local/artifact data durable;
3. upload immutable artifacts to MinIO;
4. persist artifact references;
5. publish the job with a stable inspection/job identity;
6. let the Worker write stages, results, and events idempotently;
7. mark the inspection completed only after the final result is durable.

Use an outbox/inbox or a reconciliation process for failures between database
commit and message publication. Never hold a PostgreSQL transaction while
calling MinIO, RabbitMQ, camera code, or OCR.

## PostgreSQL indexes

Start with indexes justified by actual dashboard and worker queries:

~~~text
inspections: (triggered_at DESC, id DESC)
inspection_stages: primary key (inspection_id, stage_key)
inspection_results: (inspection_id, result_type, result_index)
inspection_events: (inspection_id, sequence_no)
inspection_artifacts: (inspection_id, artifact_type, ordinal)
~~~

The unique key on inspection_results may already support lookup by
inspection_id; do not automatically add a redundant index.

Use EXPLAIN (ANALYZE, BUFFERS) and query statistics before adding broad JSONB
or low-selectivity status indexes. Add JSONB indexes only for known, frequent
queries.

## Alternatives considered

### Keep all stage state in inspections.stage_states JSONB

Rejected as the long-term source of truth because multiple writers can lose
updates and stage-level queries become difficult. It is acceptable as a
temporary denormalized projection during migration.

### Create a separate table for every stage

Rejected for the initial design because it creates unnecessary schema churn.
One inspection_stages table with a constrained stage_key is sufficient.

### Put image and raw model output directly in PostgreSQL

Rejected because large binary/JSON payloads increase database size, backup cost,
and update overhead. MinIO remains the binary/object store.

## Consequences

Positive:

- independent Station and Worker updates;
- clearer ownership and retry behavior;
- deterministic timeline ordering;
- idempotent artifact and event handling;
- better queryability and future metrics aggregation;
- smaller mutable rows in inspections.

Costs:

- one or two additional tables compared with the original JSONB-only proposal;
- more explicit repository methods and migrations;
- the project must implement PostgreSQL integration before this architecture
  can be tested end to end.

## Implementation order

1. Map current InspectionJob, InspectionResult, LocalSpool status, MinIO
   artifact references, and Rabbit messages to the proposed records.
2. Decide whether results are single-current or multi-attempt.
3. Define stage ownership and lifecycle transitions.
4. Create DDL/migrations with defaults, foreign keys, checks, and indexes.
5. Add a PostgreSQL adapter and connection pool.
6. Integrate Station writes and Worker idempotent updates.
7. Add event deduplication and artifact reconciliation.
8. Add dashboard queries with cursor pagination.
9. Test duplicate messages, worker crashes, MinIO failures, and concurrent
   updates before calling the schema production-ready.

## Current conclusion

The original database design is a good conceptual baseline, but it should not
be migrated directly into production in its current form. The core change
required is to make stage state and artifact metadata independently addressable
instead of treating them as one mutable JSONB document inside inspections.

No PostgreSQL schema or runtime code is changed by this ADR.
