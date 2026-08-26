# Phase 2 Whole-Change Code Review

Review method: `code-review-and-quality` multi-axis review plus focused TDD,
Ruff static analysis, full non-runtime regression, compileall and diff checks.

REVIEW STATUS: **REQUEST CHANGES**

CRITICAL OPEN: **0** (R1 locally remediated; Linux acceptance pending)

HIGH OPEN: **0** (local adversarial verification; real services pending)

SAFE TO MERGE: **NO**

SAFE TO RUN GX10 ACCEPTANCE: **NO**

Observed final evidence: scoped Ruff passed; non-runtime pytest completed with
`316 passed, 1 skipped, 3 deselected`; three critical regression probes and
the R9 operational focused suite passed;
`compileall`, source-layout import smoke, station CLI help, and
`git diff --check` passed. The skip is the Windows-only inability to create the
symlink required by the containment test.

## Review axes

- Correctness and contracts
- Durability, idempotency and crash recovery
- Security, path containment and secret handling
- Concurrency and lifecycle ownership
- RabbitMQ ACK/confirm/retry semantics
- MinIO immutable-write semantics
- Station/worker dependency boundaries
- Observability and timing truthfulness
- Compatibility and business-semantic preservation
- Test quality and operational documentation

## Findings fixed during review

1. Removed station's startup MinIO probe to preserve offline local-spool
   availability.
2. Isolated per-record dispatch failures so one event cannot starve later jobs.
3. Added `StationTriggerFailure` to preserve event/trigger identity and a safe
   error when post-acceptance local commit fails.
4. Tightened result schema to terminal states; `COMPLETED` now requires a
   business status, closing an invalid-result idempotency/ACK hole.
5. Rejected NaN/Infinity and enabled strict canonical JSON.
6. Made Phase 2 fail fast unless PP-OCRv6 resident lifecycle is configured.
7. Wired bounded retry schedule from env into both topology and worker policy,
   with a mismatch guard.
8. Replaced vague extractor provenance with actual profile version, mapping
   summary and semantic-blocker constants.
9. Added truthful spool/upload/publish/queue/checksum/worker/end-to-end timing.
10. Added event-level station dispatch/publish lifecycle logs.
11. Ran Ruff and fixed import/type/export/unused-code issues. Intentional broad
    catches are documented only at plugin/process/recovery boundaries.

## Remaining non-code blockers

- Verify pinned minio-py `_put_object` compatibility and server conditional
  create behavior against the deployed GX10/MinIO versions.
- Verify real RabbitMQ confirms, TTL/DLX, reconnect and unacked redelivery.
- Run Linux symlink containment test; Windows lacked symlink-create privilege.
- Install and exercise the systemd unit on the target host; the repository
  contains the supervisor contract but it is not deployed by this review.
- Review the generated machine-native `uv.lock`; exact MinIO/Pika versions are
  controlled in `pyproject.toml` and their transitive graph is locked.
- Approve retention before any cleanup implementation.
- Confirm Nvidia/Customer/Our Part Number mapping; implementation is unchanged.

## Historical findings addressed by the remediation pass

- **R1 — CRITICAL:** directory `open`/`fsync` failures are swallowed by the
  local spool, so the controller can report `durable_local=true` before Linux
  directory durability has been established.
- **R2 — HIGH:** a terminal result can remain stuck in `ARTIFACTS_READY` after
  a crash because delivery-state recovery only progresses `LOCAL_ONLY` and
  `PUBLISHED` currently overloads incompatible terminal meanings.
- **R3 — HIGH:** the worker trusts Rabbit-supplied artifact and result
  bucket/key locations instead of enforcing a configured event namespace.
- **R4 — HIGH:** Rabbit `message_id`/`correlation_id` can disagree with the
  job body's `event_id` without rejection before processing.
- **R5 — HIGH:** result provenance can repeat producer claims rather than
  describing the actual worker runtime and compatibility is not enforced
  before inference.
- **R6 — HIGH:** malformed booleans and non-finite/out-of-range numeric config
  can silently weaken runtime security or lifecycle behavior.
- **R7 — HIGH:** unexpected post-trigger preparation exceptions can escape the
  controller boundary without preserving `event_id` and `trigger_id` in a
  structured terminal result.
- **R8 — HIGH:** Rabbit messages and downloaded artifacts do not have complete
  pre-parse/pre-decode size and content-type limits.
- **R9 - MEDIUM/operations:** Rabbit connection lifecycle, exclusive timing
  semantics, bucket provisioning/least privilege, dependency reproducibility,
  private MinIO API compatibility, and the test-only message-handler path need
  explicit remediation and real-service verification. The local operational
  contract is now implemented; real-infrastructure verification remains open.

The previously observed local test, Ruff, compile and diff-check evidence above
is preserved as historical evidence only. It did not exercise these adversarial
conditions and does not override the current `REQUEST CHANGES` verdict.
