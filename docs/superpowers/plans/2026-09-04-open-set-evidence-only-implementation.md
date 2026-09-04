# Open-Set Evidence-Only Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make unprofiled, unapproved, and unrecognized inspection paths preserve raw evidence and return fail-closed `REVIEW`, while only an explicitly approved, identity-consistent profile with a matching `KNOWN` document-recognition result can publish canonical fields or authorize `PASS`.

**Architecture:** Add an immutable `ProfileBinding` as the single profile contract. Build the binding explicitly in profile resolution, pass it to extractor and validator, assert the processor components agree, and use it for worker provenance compatibility. The processor will only invoke semantic extraction when the binding is approved and a matching `DocumentRecognitionResult` has status `KNOWN`; all other paths retain evidence and validate only technical stages.

**Tech Stack:** Python 3.10+, dataclasses, pytest, existing local station/worker contracts, Local Spool and JSON provenance.

**Spec:** `docs/superpowers/specs/2026-09-04-open-set-evidence-only-design.md`

**Implementation status:** Code, focused tests, full regression tests, and
documentation are complete in the working tree. Git commit/branch operations
are currently blocked because `.git` is read-only in this environment; the
commit steps below remain intentionally unexecuted.

**Post-review hardening:** The working tree also enforces the same semantic
gate at the durable worker boundary, rejects runtime descriptor drift before
new inference, preserves exact profile-version identity, persists the complete
document-recognition contract, keeps successful PASS reasons empty, rejects
unsafe pre-existing durable results, and prevents approved profiles from
inheriting process-wide required-field/barcode settings.

## Global Constraints

- A profile is never approved by absence of semantic blockers.
- A requested profile is only a station hint; it is not document recognition.
- `NVIDIA P/N` must not be emitted as `customer_part_number` in the production result while business confirmation is pending.
- Profile-free and unapproved processing must preserve OCR/barcode evidence and return `REVIEW`, not `PASS` or `FAIL`, when technical stages succeed.
- Technical OCR, barcode, quality, image and contract failures remain `ERROR`.
- Extractor, validator, processor and provenance must use the same profile name/version/approval binding.
- Do not add database tables, JWT/authentication, ERP integration, document clustering, or multi-document business policy.
- Do not stage or modify existing untracked user data.

## File Map

- Create `src/label_inspection/contracts/profile.py`: immutable profile identity and approval contract.
- `DocumentRecognitionResult` in the profile contract: an explicit, profile-bound recognition result; no recognizer implementation is added.
- Modify `src/label_inspection/contracts/__init__.py`: export the profile contract.
- Modify `src/label_inspection/extraction/fields.py`: retain and expose the binding while preserving legacy extraction construction.
- Modify `src/label_inspection/extraction/profiles.py`: build explicit unapproved/approved bindings; DGX remains unapproved.
- Modify `src/label_inspection/validation/rules.py`: consume the binding and derive the safe approval flag from it.
- Modify `src/label_inspection/app.py`: stop deriving approval from semantic blockers and disable profile validation unless explicitly approved.
- Modify `src/label_inspection/worker/processor.py`: assert binding consistency and skip semantic extraction unless the binding is approved and a matching `KNOWN` document-recognition result exists.
- Modify `src/label_inspection/worker/provenance.py`: use binding identity, expose the recognition gate in runtime provenance, and keep the worker pipeline contract version stable.
- Modify `scripts/run_station.py`: emit profile identity using the v2 provenance contract and keep profile-free requests null.
- Modify `src/label_inspection/schemas.py` and `src/label_inspection/extraction/evidence.py`: snapshot evidence containers.
- Modify `src/label_inspection/config.py`: make profile-free default explicit and keep legacy aliases validated.
- Modify `docs/CURRENT_SYSTEM_ARCHITECTURE.md` and `README.md`: document evidence-only behavior and migration boundary.
- Modify focused tests under `tests/`: cover approval, binding consistency, evidence-only results, provenance, migration and serialization.

## Task 1: Add the explicit profile binding contract

**Files:**
- Create: `src/label_inspection/contracts/profile.py`
- Modify: `src/label_inspection/contracts/__init__.py`
- Test: `tests/test_profile_binding.py`

**Interfaces:**
- Produces `ProfileBinding(name, version, approval_status)`.
- Produces `ProfileBinding.unprofiled()`.
- Produces `ProfileBinding.allows_automated_pass` and `ProfileBinding.to_dict()`.

- [ ] **Step 1: Write failing tests**

  Test that a named binding with no explicit approval is `UNAPPROVED`, a
  binding with `APPROVED_FOR_AUTOMATED_PASS` allows automated pass, null name
  cannot be approved, and one-sided name/version identity is rejected.

- [ ] **Step 2: Run focused tests and confirm RED**

  Run: `python -m pytest -q tests/test_profile_binding.py`

  Expected: collection or assertion failure because `ProfileBinding` does not
  yet exist.

- [ ] **Step 3: Implement the minimal immutable contract**

  Use a frozen dataclass with exact approval values, validate identity in
  `__post_init__`, expose `allows_automated_pass`, and serialize approval
  status without exposing implementation objects.

- [ ] **Step 4: Run focused tests and confirm GREEN**

  Run: `python -m pytest -q tests/test_profile_binding.py`

- [ ] **Step 5: Commit the slice when Git metadata is writable**

  Use: `git add src/label_inspection/contracts/profile.py src/label_inspection/contracts/__init__.py tests/test_profile_binding.py; git commit -m "feat: add explicit profile approval binding"`

## Task 2: Wire binding identity through extraction, validation and processor

**Files:**
- Modify: `src/label_inspection/extraction/fields.py`
- Modify: `src/label_inspection/extraction/profiles.py`
- Modify: `src/label_inspection/validation/rules.py`
- Modify: `src/label_inspection/app.py`
- Modify: `src/label_inspection/worker/processor.py`
- Test: `tests/test_config_wiring.py`, `tests/test_validation.py`, `tests/test_phase2_boundaries.py`

**Interfaces:**
- `FieldExtractor.profile_binding` and `LabelValidator.profile_binding` expose the same `ProfileBinding` type.
- `InspectionProcessor.profile_binding` is the binding used for one execution.
- `InspectionProcessor.__init__` raises `ValueError` when extractor and validator bindings differ.

- [ ] **Step 1: Add failing tests for approval inference and mismatch**

  Add a profile with no blockers and assert it remains unapproved; construct
  an extractor and validator with different profile identity and assert
  `InspectionProcessor` rejects them; assert `build_pipeline` does not pass
  `profile_approved=True` merely because the blocker map is empty.

- [ ] **Step 2: Run focused tests and confirm RED**

  Run: `python -m pytest -q tests/test_config_wiring.py tests/test_validation.py tests/test_phase2_boundaries.py`

- [ ] **Step 3: Implement binding propagation and consistency checks**

  Build the binding explicitly in `build_extractor`, pass it into the
  validator, derive approval only from its approval status, and assert exact
  extractor/validator equality in the processor. Preserve legacy constructor
  arguments by translating them to an unapproved binding unless callers
  explicitly request approval.

- [ ] **Step 4: Run focused tests and confirm GREEN**

  Run: `python -m pytest -q tests/test_config_wiring.py tests/test_validation.py tests/test_phase2_boundaries.py`

- [ ] **Step 5: Commit the slice when Git metadata is writable**

  Use: `git add src/label_inspection/extraction/fields.py src/label_inspection/extraction/profiles.py src/label_inspection/validation/rules.py src/label_inspection/app.py src/label_inspection/worker/processor.py tests/test_config_wiring.py tests/test_validation.py tests/test_phase2_boundaries.py; git commit -m "fix: enforce one profile binding across processing"`

## Task 3: Enforce evidence-only behavior for unapproved profiles

**Files:**
- Modify: `src/label_inspection/worker/processor.py`
- Modify: `src/label_inspection/validation/rules.py`
- Test: `tests/test_open_set_processing.py`, `tests/test_validation.py`, `tests/test_phase2_end_to_end.py`

**Interfaces:**
- Approved binding plus matching `KNOWN` document recognition: semantic extraction runs and profile validation may produce `PASS`, `REVIEW`, or `FAIL`.
- Unapproved/unprofiled binding: `extracted == {}` and successful technical processing returns `REVIEW` with `NO_APPROVED_PROFILE`.

- [ ] **Step 1: Write failing behavior tests**

  Add tests that feed DGX-like OCR lines through an unapproved DGX processor
  and assert `extracted == {}`, the raw line remains in `evidence`, and the
  result cannot contain `customer_part_number`. Add a test with all legacy
  required values present and assert the result still cannot be `PASS`.

- [ ] **Step 2: Run focused tests and confirm RED**

  Run: `python -m pytest -q tests/test_open_set_processing.py tests/test_validation.py tests/test_phase2_end_to_end.py`

- [ ] **Step 3: Implement the evidence-only gate**

  Keep OCR, barcode and `collect_evidence` unconditional. Run field
  extraction only when `profile_binding.allows_automated_pass` is true. For
  unapproved bindings pass no profile required-field/barcode policy into the
  validator, then retain the existing technical `ERROR` paths and force only
  successful non-error outcomes to `REVIEW`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

  Run: `python -m pytest -q tests/test_open_set_processing.py tests/test_validation.py tests/test_phase2_end_to_end.py`

- [ ] **Step 5: Commit the slice when Git metadata is writable**

  Use: `git add src/label_inspection/worker/processor.py src/label_inspection/validation/rules.py tests/test_open_set_processing.py tests/test_validation.py tests/test_phase2_end_to_end.py; git commit -m "fix: keep unapproved profiles evidence-only"`

## Checkpoint: Semantic safety

- [ ] Profile-free and unapproved tests pass.
- [ ] A high-confidence `NVIDIA P/N` observation is preserved as raw evidence and never emitted as a canonical field.
- [ ] Technical failure tests still return `ERROR`.

## Task 4: Harden provenance, migration and evidence snapshots

**Files:**
- Modify: `src/label_inspection/worker/provenance.py`
- Modify: `scripts/run_station.py`
- Modify: `src/label_inspection/schemas.py`
- Modify: `src/label_inspection/extraction/evidence.py`
- Modify: `src/label_inspection/config.py`
- Test: `tests/test_open_set_provenance.py`, `tests/test_phase2_worker.py`, `tests/test_schema.py`, `tests/test_config_wiring.py`

**Interfaces:**
- `WorkerRuntimeDescriptor` contains one serialized profile binding and uses it for compatibility.
- `WORKER_PIPELINE_VERSION` becomes `phase2-worker.v2`.
- Profile-free station provenance contains `requested_profile: null`.
- Legacy `requested_profile={"name":"default","version":"1.0"}` is accepted only as a safe profile-free alias by v2 workers.

- [ ] **Step 1: Write failing contract tests**

  Add tests for v2 profile binding provenance, extractor/validator mismatch,
  null profile requests, safe legacy default alias handling, and evidence
  snapshot stability after mutating source polygon/metadata objects.

- [ ] **Step 2: Run focused tests and confirm RED**

  Run: `python -m pytest -q tests/test_open_set_provenance.py tests/test_phase2_worker.py tests/test_schema.py tests/test_config_wiring.py`

- [ ] **Step 3: Implement the versioned compatibility and snapshots**

  Make provenance compatibility compare the binding rather than extractor
  fields alone, bump the pipeline version, add station migration metadata as
  an additive provenance field, and use `freeze_json`/`thaw_json` or equivalent
  immutable snapshots for evidence polygon/metadata. Change the Settings
  fallback to explicit `none` while retaining validation for legacy aliases.

- [ ] **Step 4: Run focused tests and confirm GREEN**

  Run: `python -m pytest -q tests/test_open_set_provenance.py tests/test_phase2_worker.py tests/test_schema.py tests/test_config_wiring.py`

- [ ] **Step 5: Commit the slice when Git metadata is writable**

  Use: `git add src/label_inspection/worker/provenance.py scripts/run_station.py src/label_inspection/schemas.py src/label_inspection/extraction/evidence.py src/label_inspection/config.py tests/test_open_set_provenance.py tests/test_phase2_worker.py tests/test_schema.py tests/test_config_wiring.py; git commit -m "fix: version open-set profile provenance contract"`

## Task 5: Complete end-to-end coverage and documentation

**Files:**
- Modify: `tests/test_phase2_station_service.py`, `tests/test_phase2_end_to_end.py`, `tests/test_open_set_processing.py`
- Modify: `docs/CURRENT_SYSTEM_ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- Station profile-free job -> worker profile-free processor -> durable result is a tested path.
- Documentation states that only explicitly approved profiles can publish canonical semantic fields or `PASS`.

- [ ] **Step 1: Add the station-to-worker regression test**

  Build a profile-free station job with `requested_profile: null`, process it
  with a profile-free worker, and assert evidence survives into the durable
  result, `extracted` is empty, and business status is `REVIEW`.

- [ ] **Step 2: Run the new integration test and confirm RED**

  Run: `python -m pytest -q tests/test_phase2_station_service.py tests/test_phase2_end_to_end.py`

- [ ] **Step 3: Implement only the required documentation and fixture wiring**

  Replace stale default-SKU claims, explain the draft/unapproved DGX profile,
  document the v2 rollout order, and keep the existing no-auth/no-ERP scope.

- [ ] **Step 4: Run the integration tests and confirm GREEN**

  Run: `python -m pytest -q tests/test_phase2_station_service.py tests/test_phase2_end_to_end.py`

- [ ] **Step 5: Commit the slice when Git metadata is writable**

  Use: `git add tests/test_phase2_station_service.py tests/test_phase2_end_to_end.py tests/test_open_set_processing.py docs/CURRENT_SYSTEM_ARCHITECTURE.md README.md .env.example; git commit -m "docs: define open-set evidence-only rollout"`

## Final Checkpoint: Release gate

- [ ] Run `python -m pytest -q` and record the exit code and skip reasons.
- [ ] Run `python -m compileall -q src scripts`.
- [ ] Inspect `git diff` and confirm no user untracked data is staged.
- [ ] Run a final false-PASS test with unapproved DGX evidence.
- [ ] Request an independent code review before merge.
- [ ] If Git metadata remains read-only, report the exact commit commands for the user to run locally; do not claim a commit was created.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A future profile omits approval metadata | False `PASS` | Binding defaults to `UNAPPROVED`; approval is an explicit enum value. |
| Extractor and validator are configured differently | Wrong business semantics | Processor rejects unequal bindings before processing. |
| Old station/worker versions coexist | Job rejection or unsafe legacy semantics | Bump v2 provenance, safe legacy read on v2 worker, deploy workers before station, drain old jobs. |
| Evidence aliases mutable OCR structures | Audit evidence changes after collection | Snapshot JSON metadata into immutable containers. |
| Existing consumers expect default SKU/LOT extraction | Behavior change to `REVIEW` | Explicit `none` default, migration note and test coverage. |

## Open Questions

- None for this implementation scope. Business confirmation of DGX semantic mappings remains outside runtime implementation and is required before any profile is marked approved.
