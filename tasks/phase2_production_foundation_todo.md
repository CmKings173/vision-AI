# Phase 2 Production Foundation V1 — Execution Checklist

Status: `CHECKPOINTS 2A-2F IMPLEMENTED/TESTED LOCALLY - GX10 RUNTIME PENDING`

Source of truth: `tasks/phase2_production_foundation_technical_design.md`

Local verification authority:

- `tasks/phase2_implementation_report.md`
- `tasks/phase2_code_review.md`

Checklist clarification: the dispatcher transition, confirmed publish
transition, sequential worker pipeline, and exact crop/checksum/result trace
are implemented and covered by deterministic local tests. Rows that explicitly
require deployed MinIO/RabbitMQ, persistent GX10 processes, or real-sample
latency characterization remain intentionally unchecked until runtime evidence
is collected.

## Pre-implementation gate

- [x] Inspect repository structure, config, dependencies, scripts and tests.
- [x] Trace active RTSP-to-result runtime.
- [x] Map design components as reuse/refactor/new.
- [x] Record gaps, risks, invariants and true blockers.
- [x] Create Phase 2-specific plan without overwriting the overall roadmap.
- [x] Human approved the audit, clarifications and checkpoint plan.

## 2A — Contracts and boundaries

- [x] Test UUID event/trigger identity and epoch-ms fields.
- [x] Encode `received_at_ms` and optional trustworthy `source_timestamp_ms` semantics.
- [x] Add versioned artifact/job/result/error contracts.
- [x] Separate processing, delivery and business status domains and ownership.
- [x] Represent quality-rejected terminal result without inference job.
- [x] Represent preparation-error terminal result without business `FAIL` or inference job.
- [x] Test exact-crop ownership.
- [x] Split station preparation from worker processing.
- [x] Split app factories; station factory must not load OCR/ZXing.
- [x] Preserve compatibility for `InspectionPipeline` and diagnostic scripts.
- [x] Run focused tests, relevant regressions, compileall and diff checks.
- [x] Report observed evidence and stop for checkpoint 2A review.

## 2B — Atomic local spool

- [x] Test path containment, traversal and symlink escape.
- [x] Implement atomic temp-directory to final-directory commit.
- [x] Persist `selected_frame.jpg`, exact `label_crop.png`, `job.json`, `state.json` for inference jobs.
- [x] Freeze immutable `job.json` before local commit.
- [x] Persist terminal `result.json` path for quality rejection.
- [x] Persist terminal `result.json` path for preparation technical error.
- [x] Add SHA-256 and content metadata.
- [x] Add atomic delivery-state transitions.
- [x] Add startup scan/recovery and corrupt/incomplete handling.
- [x] Add backpressure/disk threshold fail-closed behavior.
- [x] Keep automatic spool deletion disabled.
- [x] Run failure simulations, regressions, compileall and diff checks.
- [x] Human review checkpoint 2B and authorization to complete Phase 2.

## 2C — MinIO

- [x] Select and pin a pure-Python client candidate; GX10 verification pending.
- [x] Add storage abstraction.
- [x] Add MinIO adapter and deterministic bucket/key layout.
- [x] Add checksum-idempotent upload and conflict detection.
- [x] Upload inference artifacts and terminal station results.
- [ ] Implement dispatcher `LOCAL_ONLY → ARTIFACTS_READY`.
- [x] Add secret-redaction tests.
- [ ] Run outage/restart integration test.
- [x] Update `.env.example` and runbook.

## 2D — RabbitMQ and station

- [x] Select and pin a client supporting publisher confirms/manual ACK.
- [x] Add durable exchange/routing/queue/DLQ-compatible topology.
- [x] Publish persistent messages with confirms.
- [x] Publish exact frozen `job.json` without reconstruction.
- [ ] Implement `ARTIFACTS_READY → PUBLISHED` only after confirm.
- [x] Implement persistent station controller/entrypoint.
- [x] Verify station does not import/build/call OCR runtime.
- [ ] Run broker outage/restart integration test.

## 2E — Resident worker

- [x] Load/warm PP-OCRv6 and ZXing once before READY.
- [x] Use prefetch 1 and manual ACK.
- [x] Validate job, download exact crop and verify checksum.
- [x] Validate existing durable result for logical idempotency.
- [ ] Preserve sequential OCR → ZXing → extraction → validation.
- [x] Persist versioned durable result before ACK.
- [x] Preserve current extractor/profile semantics.
- [x] Record profile/locator/extractor versions and semantic blocker.
- [ ] Run worker restart/redelivery integration tests.

## 2F — Reliability and GX10 acceptance

- [x] Add configurable bounded retry, default 5/30/120 seconds.
- [x] Add retry classification and max-attempt policy.
- [x] Route exhausted/non-retryable jobs to final DLQ.
- [x] Execute local deterministic crash-point fault-injection matrix.
- [x] Verify simulated MinIO/Rabbit recovery and disk backpressure.
- [x] Add structured lifecycle logs and event trace.
- [ ] Run 10-trigger functional E2E smoke in persistent processes.
- [ ] Use at least 30 samples for percentile characterization; prefer 50–100.
- [ ] Verify exact crop/checksum/result trace.
- [x] Mark each claim IMPLEMENTED/TESTED/RUNTIME VERIFIED/NOT VERIFIED.

## Confirmed out of scope

- YOLO/custom detector
- VLM/GLM
- TensorRT conversion or optimization
- PostgreSQL, Redis, Kafka and Kubernetes
- Business mapping changes for Nvidia/Customer/Our Part Number
- Full Phase 1 dataset benchmark
- Automatic deletion of committed spool evidence

## Known blockers and prerequisites

- `KNOWN_SEMANTIC_BLOCKER`: Nvidia P/N ↔ Customer Part Number ↔ Our Part Number needs business confirmation; it does not block infrastructure.
- MinIO/RabbitMQ endpoints and safe credentials are required before runtime 2C/2D, not before 2A/2B.
- Retention/cleanup policy is not approved; default is no deletion.
- The local audit Python initially lacked `pytest`; each actually executed test command must be reported without inferring success.
