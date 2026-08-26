# Phase 2 Security / Reliability Remediation Checklist

This checklist is the source of truth for the remediation pass. Status may be
changed to `CLOSED` only after the named adversarial test and relevant Phase 2
regressions have passed. Real-service items remain open until observed against
actual MinIO and RabbitMQ instances.

Current gate:

- REVIEW STATUS: `REQUEST CHANGES`
- CRITICAL OPEN: `0` (local adversarial verification; GX10/Linux acceptance not run)
- HIGH OPEN: `0` (local adversarial verification; real services still gated)
- SAFE TO MERGE: `NO`
- SAFE TO RUN GX10 ACCEPTANCE: `NO`

| ID | Severity | Affected files | Required behavior | Tests to add | Implementation change | Verification result | Status |
|---|---|---|---|---|---|---|---|
| R1 | CRITICAL | `station/spool.py`, controller and spool tests | Linux durable success only after file flush/fsync, rename and parent-directory fsync; preserve IDs and return structured failure otherwise | File fsync, rename, parent open, parent fsync failures; successful sequence; controller never reports durable on failures | Explicit POSIX-required/Windows-best-effort directory durability policy; propagated `SPOOL_COMMIT_ERROR` | RED: 2 expected parent-directory failures; GREEN: 35 passed/1 Windows symlink skip; regressions: 48 passed/1 skip | CLOSED (LOCAL) |
| R2 | HIGH | delivery contracts, spool, dispatcher, publisher/pump and tests | Explicit inference and terminal-result end states; resume every durable intermediate state; never publish terminal result as a job | Restart from `LOCAL_ONLY` and `ARTIFACTS_READY`, both record kinds, terminal no-op and incompatible states | Split `JOB_PUBLISHED` and `TERMINAL_RESULT_DURABLE`; state-aware resume and legacy read migration | RED: 12 failed/54 passed/1 skip, plus 2 incompatible-state failures; GREEN: 68 passed/1 skip | CLOSED (LOCAL) |
| R3 | HIGH | storage key policy, worker, worker/storage tests | Enforce configured bucket, deterministic station/event label-crop key, PNG type/size/checksum; derive result location in worker | Other bucket/key/event, traversal, wrong type, oversize, malformed hash, result injection before OCR/storage | Shared `ArtifactKeyPolicy` and worker-side result destination derivation | RED: 6 failed/2 passed; GREEN: 8 passed; regressions: 32 passed, then 20 passed after import refactor | CLOSED (LOCAL) |
| R4 | HIGH | retry/consumer handler, worker entrypoint and messaging tests | Required `message_id == body.event_id`; present correlation ID must match; poison message is non-retryable and does no I/O/inference | Matching, mismatching, missing message ID and correlation mismatch | Validate canonical transport/body identity before worker dispatch and route violations to confirmed DLQ | RED: 4 failed; GREEN: 4 passed; regressions: 30 passed | CLOSED (LOCAL) |
| R5 | HIGH | station producer provenance, worker runtime descriptor, inference worker/tests | Separate producer request from actual worker runtime; reject incompatible profiles before inference; generate result provenance at worker | Compatible and incompatible profiles; actual component descriptor; same-process durable-result reuse | Add `WorkerRuntimeDescriptor`, requested-profile compatibility gate and actual result provenance | RED: 2 failed; GREEN: 2 passed; regressions initially 1 failed/59 passed then 60 passed | CLOSED (LOCAL) |
| R6 | HIGH | config parser/validation and config/runtime tests | Strict finite boolean grammar; finite constrained safety numeric values; invalid config fails startup | Accepted grammar, invalid spellings, NaN/Inf/-Inf, negative/zero/>60 interval, direct non-finite settings | Central `ConfigError`, strict bool/int/float parsers and finite validation | RED: import/collection error for missing fail-closed contract; GREEN: 24 passed; regressions: 64 passed | CLOSED (LOCAL) |
| R7 | HIGH | preparation outcome, inspection controller and station tests | Create identities at trigger acceptance; normalize every normal downstream exception; preserve processing and spool failures | Selector `RuntimeError`; terminal spool failure; `BaseException` not caught | Controller-level `Exception` boundary, terminal `INTERNAL_ERROR`, dual-error trigger failure | RED: 2 failed/1 passed; GREEN: 3 passed; regressions: 48 passed/1 skip | CLOSED (LOCAL) |
| R8 | HIGH | Rabbit handler, config, storage adapters, worker and tests | Reject oversized body before JSON parse; stat/type/size before download; bounded stream; pixel cap before decode/inference | Oversized body/HEAD/actual stream, wrong type, oversized PNG dimensions | Configured limits, HEAD-first metadata validation, bounded reads and PNG IHDR gate | RED: 4 failures then 2 failures; GREEN: 4+2 passed; regressions: 77 passed, then 36 passed | CLOSED (LOCAL) |
| R9 | MEDIUM | worker runtime, timing, storage startup, dependencies, runbook/tests | Explicit reconnect or supervisor contract; exclusive timings; startup bucket validation; reproducible dependency graph; runtime/tests share handler | Lifecycle/timing/readiness probes plus real-service integration matrix | Focused operational hardening without CV/business changes | Local operational contract GREEN; real MinIO/RabbitMQ/supervisor verification pending | CLOSED (LOCAL) / OPEN (REAL INFRA) |

## Slice evidence

Each completed slice records the following fields here or in a linked report:

- FINDING
- STATUS
- ROOT CAUSE
- FILES CHANGED
- TEST ADDED
- BEFORE FIX
- AFTER FIX
- COMMANDS RUN
- OBSERVED RESULT
- RUNTIME VERIFIED
- REMAINING RISK

No GX10 acceptance or production-readiness claim is permitted during this pass.

## R1 durability

- FINDING: directory-fsync durability false positive
- STATUS: `CLOSED (LOCAL)`; real GX10/Linux acceptance was not run
- ROOT CAUSE: `_fsync_directory()` discarded both directory-open and
  directory-fsync failures while the controller unconditionally constructed a
  report with `durable_local=true` after `commit_outcome()` returned.
- FILES CHANGED: `src/label_inspection/station/spool.py`,
  `tests/test_phase2_spool.py`, `tests/test_phase2_station_service.py`
- TEST ADDED: file-fsync failure, parent-directory open failure,
  parent-directory fsync failure, ordered successful sequence and four
  controller durability-fault cases; existing rename probe retained.
- BEFORE FIX: `python -m pytest tests/test_phase2_spool.py
  tests/test_phase2_station_service.py -q` produced `2 failed, 33 passed,
  1 skipped`; both failures were `DID NOT RAISE SpoolCommitError` for parent
  open/fsync.
- AFTER FIX: POSIX directory durability errors propagate as
  `SPOOL_COMMIT_ERROR`; Windows development has an explicit best-effort branch;
  the controller cannot return a successful report for tested A-D faults.
- COMMANDS RUN: focused command above; then `python -m pytest
  tests/test_phase2_boundaries.py tests/test_phase2_spool.py
  tests/test_phase2_station_service.py tests/test_phase2_delivery_pump.py
  tests/test_phase2_dispatcher.py -q`.
- OBSERVED RESULT: focused `35 passed, 1 skipped`; relevant regressions
  `48 passed, 1 skipped`. The skip is Windows symlink privilege, unrelated to
  durability probes.
- RUNTIME VERIFIED: `NO` — tests forced the required policy on Windows; GX10
  execution is intentionally deferred by the remediation gate.
- REMAINING RISK: Linux filesystem/hardware behavior still requires the later
  acceptance environment; no code path may claim global production readiness.

## R2 dispatch recovery

- FINDING: terminal records could remain permanently at `ARTIFACTS_READY` and
  `PUBLISHED` overloaded two delivery outcomes.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: dispatcher returned early for every state other than
  `LOCAL_ONLY`; the shared terminal enum could not express whether Rabbit
  publication or terminal-result object durability had completed.
- FILES CHANGED: delivery contract, spool model/recovery, dispatcher,
  publisher, delivery pump and their focused tests.
- TEST ADDED: terminal restart at `ARTIFACTS_READY`, inference restart at
  `ARTIFACTS_READY`, terminal idempotent no-op, terminal never creates an
  inference publisher, and incompatible record/status rejection. Existing
  local-only restart and confirm-before-transition probes remain active.
- BEFORE FIX: focused R2 command produced `12 failed, 54 passed, 1 skipped`;
  the state-domain and every new explicit terminal-state assertion failed.
  A subsequent boundary probe produced `2 failed` because incompatible
  record/status pairs were accepted.
- AFTER FIX: inference ends in `JOB_PUBLISHED`; station-terminal delivery ends
  in `TERMINAL_RESULT_DURABLE`; `ARTIFACTS_READY` is resumed according to
  record kind; only inference records can activate the publisher. Old
  `PUBLISHED` state files are read through a record-kind migration and are
  never newly emitted.
- COMMANDS RUN: `python -m pytest tests/test_phase2_contracts.py
  tests/test_phase2_spool.py tests/test_phase2_dispatcher.py
  tests/test_phase2_messaging.py tests/test_phase2_delivery_pump.py
  tests/test_phase2_end_to_end.py -q`; incompatible-state test was also run
  alone during RED.
- OBSERVED RESULT: `68 passed, 1 skipped`; skip is the unrelated Windows
  symlink-privilege probe.
- RUNTIME VERIFIED: `NO` — no real RabbitMQ was used.
- REMAINING RISK: actual publisher-confirm/redelivery behavior remains part of
  the required real-infrastructure gate.

## R3 worker artifact trust boundary

- FINDING: worker accepted Rabbit-supplied bucket/key/type/destination claims.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: parsed `ArtifactRef` values were passed directly to storage and
  the result bucket was copied from the untrusted label-crop reference.
- FILES CHANGED: storage policy/errors/exports, inference worker, worker
  entrypoint, and adversarial worker tests.
- TEST ADDED: other bucket, unrelated key, another event key, traversal key,
  wrong content type, oversized declaration, malformed SHA-256 and injected
  result destination. Every case asserts zero storage and OCR calls.
- BEFORE FIX: attack probe produced `6 failed, 2 passed`; five attacks reached
  storage metadata lookup and injected result destination was accepted.
- AFTER FIX: `ArtifactKeyPolicy` enforces exact source artifact names,
  configured bucket, canonical station/date/event keys, JPEG/PNG types,
  structural hash and a 16 MiB declared crop ceiling before storage access;
  result bucket/key are always worker-derived.
- COMMANDS RUN: `python -m pytest tests/test_phase2_worker.py -q -k
  untrusted_artifact_location`; then worker/storage/MinIO/dispatcher/E2E focused
  regression commands.
- OBSERVED RESULT: attack probe `8 passed`; relevant regression `32 passed`;
  post-refactor worker/storage check `20 passed`.
- RUNTIME VERIFIED: `NO` — storage fake only; real MinIO remains gated.
- REMAINING RISK: actual object metadata and bounded-stream behavior are R8 and
  real-MinIO concerns, not claimed by this slice.

## R4 message identity

- FINDING: Rabbit transport IDs and job `event_id` could disagree while normal
  processing continued.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: runtime substituted `UNKNOWN_EVENT` and retry handling forwarded
  body bytes to the worker without comparing `message_id`, `correlation_id`
  and body identity.
- FILES CHANGED: retry/message handling core, worker runtime callback and
  retry/observability tests.
- TEST ADDED: matching IDs, message/body mismatch, missing message ID and
  correlation mismatch. Poison cases assert no worker call, confirmed DLQ,
  non-retryable failure metadata and ACK only after handoff.
- BEFORE FIX: identity command produced `4 failed`; handler did not accept the
  correlation argument or enforce any identity invariant.
- AFTER FIX: canonical UUID identity is checked before `process_message()`;
  mismatch raises `MESSAGE_IDENTITY_MISMATCH`, bypasses retries and uses the
  poison-message DLQ path. Runtime no longer substitutes `UNKNOWN_EVENT`.
- COMMANDS RUN: `python -m pytest
  tests/test_phase2_retry_and_observability.py -q -k identity`; then the full
  retry/messaging/worker focused set.
- OBSERVED RESULT: identity `4 passed`; relevant regressions `30 passed`.
- RUNTIME VERIFIED: `NO` — Rabbit properties/confirm behavior used fakes.
- REMAINING RISK: real broker property propagation and DLQ confirmation remain
  in the infrastructure integration gate.

## R5 actual worker provenance

- FINDING: worker copied producer provenance into result without proving it
  matched resident runtime components.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: no worker-owned runtime descriptor or requested-profile
  compatibility gate existed; the fake job claimed DGX semantics while using
  the default SKU extractor.
- FILES CHANGED: worker provenance module, inference worker, station producer
  provenance, worker/E2E tests.
- TEST ADDED: explicit default-profile compatibility, DGX/default mismatch
  before storage/OCR, and assertions for actual OCR/barcode/extractor/validator
  and pipeline provenance in durable results. The old mismatched fake job was
  corrected to request the profile it actually executes.
- BEFORE FIX: focused provenance command produced `2 failed`; result lacked
  worker-owned provenance and mismatched requested profile ran inference.
- AFTER FIX: station emits `requested_profile` separately under producer
  provenance; worker derives a resident descriptor from actual objects,
  enforces exact canonical profile name/version, emits `PROFILE_MISMATCH` as
  non-retryable, and validates descriptor equality when reusing a result.
- COMMANDS RUN: focused provenance command; then worker/E2E/retry/contracts/
  station focused regressions.
- OBSERVED RESULT: focused `2 passed`; regression first exposed one frozen-JSON
  tuple/list comparison defect (`1 failed, 59 passed`), fixed via canonical
  thaw; final `60 passed`.
- RUNTIME VERIFIED: `NO` — fake OCR/barcode components were used.
- REMAINING RISK: package-level runtime versions for real PP-OCRv6 and ZXing
  must be observed on GX10 later; no business mapping was changed.

## R6 fail-closed configuration

- FINDING: malformed booleans silently became false and non-finite dispatch
  values passed validation.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: `_bool()` was a membership test whose negative branch conflated
  explicit false with malformed input; Python float parsing and comparisons
  allow NaN to bypass ordinary range checks.
- FILES CHANGED: central config module and Phase 2 runtime-config tests.
- TEST ADDED: case-insensitive true/false/1/0/yes/no grammar; `tru`, `truee`,
  `ture`, `on`, `off`, `2`, empty; NaN/Infinity/-Infinity; negative, zero and
  >60-second dispatch intervals; direct non-finite worker safety settings.
- BEFORE FIX: focused test collection failed because no `ConfigError`/strict
  contract existed; the original probes had separately observed `tru -> False`
  and accepted NaN.
- AFTER FIX: every environment-backed integer/float uses explicit parser
  helpers; booleans accept only the documented finite grammar; all safety
  floats are finite-checked; dispatch is constrained to `(0, 60]` seconds.
- COMMANDS RUN: focused config adversarial selection; then runtime-config,
  config-wiring, station-controller and delivery-pump regressions.
- OBSERVED RESULT: adversarial `24 passed`; relevant regressions `64 passed`.
- RUNTIME VERIFIED: `YES (local process startup/config boundary)`; GX10 itself
  was not run.
- REMAINING RISK: CLI-only numeric arguments outside `Settings` retain their
  own argparse validation and are outside this finding.

## R7 controller error boundary

- FINDING: unexpected post-trigger exceptions escaped without durable result or
  traceable identities.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: controller caught only `SpoolError`; snapshot and preparer
  orchestration ran outside a normal-exception normalization boundary.
- FILES CHANGED: preparation outcome, station controller and station tests.
- TEST ADDED: selector `RuntimeError` becomes durable terminal
  `INTERNAL_ERROR`; simultaneous processing/spool failure preserves both safe
  errors and IDs with `durable_local=false`; `KeyboardInterrupt` is not caught.
- BEFORE FIX: focused command produced `2 failed, 1 passed`; raw selector
  `RuntimeError` crossed the application boundary.
- AFTER FIX: trigger IDs are created first; any normal preparation exception is
  normalized and committed as a terminal result. If that commit fails,
  `StationTriggerFailure` carries event/trigger IDs, processing error,
  durability error and explicit false durability.
- COMMANDS RUN: focused R7 selection; then station/spool/boundary/E2E tests.
- OBSERVED RESULT: focused `3 passed`; relevant regressions `48 passed,
  1 skipped` (unrelated Windows symlink privilege).
- RUNTIME VERIFIED: `YES (local controller and filesystem fake/fault paths)`;
  no GX10 run.
- REMAINING RISK: process-kill/power-loss behavior remains a later Linux
  acceptance concern; `BaseException` intentionally preserves process control.

## R8 resource limits

- FINDING: Rabbit JSON and MinIO object reads were unbounded; metadata/type was
  checked only after download and decode had no pixel ceiling.
- STATUS: `CLOSED (LOCAL)`
- ROOT CAUSE: handler parsed immediately, MinIO used `response.read()` without
  a size, and worker delegated dimensions directly to OpenCV.
- FILES CHANGED: config/env example, retry handler, storage protocol/fake/MinIO
  adapter, inference worker/entrypoint and focused tests.
- TEST ADDED: oversized body with `json.loads` observation, oversized HEAD,
  wrong content type before GET, stream larger than declared metadata, and
  400 MP PNG IHDR rejected before `cv2.imdecode`/OCR.
- BEFORE FIX: first focused set produced `4 failed` for missing message/storage
  limits; second produced `2 failed` because pixel/config limits were absent.
- AFTER FIX: body limit is 1 MiB before parse; crop limit is 16 MiB; image limit
  is 16 MP. MinIO now HEAD-validates exact immutable metadata and configured
  size before GET, reads at most declared size plus one byte, and rejects short
  or long streams. Worker validates PNG IHDR dimensions before OpenCV.
- COMMANDS RUN: two focused RED/GREEN selections; then worker, MinIO, storage,
  retry, runtime-config and E2E regressions; post-bound update focused rerun.
- OBSERVED RESULT: first GREEN `4 passed`; second GREEN `2 passed`; relevant
  regressions `77 passed`; post-update `36 passed`.
- RUNTIME VERIFIED: `NO` — fake MinIO response only.
- REMAINING RISK: actual minio-py response streaming and metadata behavior must
  pass the required real-service integration before merge approval.

## R9 operational hardening

- FINDING: worker lifecycle, timing ownership, bucket provisioning, dependency
  control, and test/runtime message-handler parity were not explicit enough for
  an operational deployment.
- STATUS: `CLOSED (LOCAL)` / `OPEN (REAL INFRA)`
- ROOT CAUSE: the runtime had no explicit supervisor contract, measured timing
  fields mixed artifact download/decode responsibilities, and the dispatcher
  could provision buckets during processing.
- FILES CHANGED: worker/station entrypoints, dispatcher, inference worker,
  storage protocol/adapters, worker timing/reporting, Phase 2 tests, systemd
  unit and operations runbook. `pyproject.toml` keeps MinIO 7.2.20 and Pika
  1.4.4 as exact controlled transport requirements.
- TEST ADDED: startup `validate_bucket` and no-per-artifact-provisioning
  checks, shared runtime retry handler check, exclusive timing assertions,
  broker-loss exit contract, and deployment-documentation checks.
- BEFORE FIX: the dispatcher/worker called bucket provisioning during runtime;
  worker timing did not expose image decode separately; the runtime had no
  explicit restart artifact; and the test-only handler remained as a separate
  path.
- AFTER FIX: bucket provisioning is installation/admin responsibility and
  runtime calls only `validate_bucket`; timing fields are exclusive; broker
  consumption failure emits `BROKER_CONNECTION_LOST` and exits for supervisor
  restart; runtime and tests use `RetryingWorkerMessageHandler`.
- COMMANDS RUN: focused R9 Phase 2 test selection; full non-runtime pytest;
  compileall; `git diff --check`; narrow Ruff; three critical regression
  probes.
- OBSERVED RESULT: focused R9 `39 passed`; full relevant suite `310 passed,
  1 skipped, 3 deselected`; compileall, diff-check and narrow Ruff passed.
- RUNTIME VERIFIED: `NO` — no real MinIO/RabbitMQ or systemd environment was
  available. `uv lock` could not be run because the local uv executable was
  blocked by the environment; exact transport pins remain in pyproject as the
  controlled reproducibility mechanism.
- REMAINING RISK: exercise private MinIO create-only upload behavior, RabbitMQ
  confirm/reconnect/redelivery, systemd restart and Linux durability on GX10
  before merge or acceptance approval.
