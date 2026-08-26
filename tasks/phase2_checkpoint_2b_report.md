# Phase 2 Checkpoint 2B Report - Atomic Local Spool

Status: **IMPLEMENTED AND TESTED LOCALLY - WAITING FOR HUMAN 2B REVIEW**

Checkpoint 2C has not started. This report makes no MinIO, RabbitMQ, worker, or
GX10 runtime claim.

## Implemented

- Station-owned `LocalSpool` with strict UUID event paths and resolved-path
  containment.
- Same-parent `.tmp_<event_id>` commit followed by atomic directory rename.
- Inference layout: `selected_frame.jpg`, lossless exact `label_crop.png`,
  immutable `job.json`, and `state.json`.
- Frozen `inspection-job.v1` contains exact artifact SHA-256, content type,
  size, deterministic station/date/event object keys, selection/quality/ROI,
  orientation degrees, and caller-supplied profile/version provenance.
- Quality-rejected terminal layout with available selected frame/crop plus
  `inspection-result.v1`; no inference job is created.
- Early preparation-error terminal layout with `result.json` and `state.json`;
  no image or inference job is invented.
- Atomic monotonic delivery transitions:
  `LOCAL_ONLY -> ARTIFACTS_READY -> PUBLISHED`; same-state retries are
  idempotent and backward/skip transitions are rejected.
- Non-destructive startup recovery scan. Valid pending/published records,
  corrupt final records, and incomplete temp directories are reported
  separately. No committed or partial evidence is deleted.
- Recovery validates strict state/result/job schemas, event identity, required
  files, byte size, SHA-256, job artifact references, and path containment.
- Configurable fail-closed limits for pending events, logical pending bytes,
  and minimum free disk. Corrupt/incomplete top-level entries count
  conservatively as pending. PUBLISHED records no longer count as pending but
  still consume disk and therefore remain covered by the free-space guard.
- Exact prospective event bytes are calculated before temp-directory creation.
- Station factory wiring through `build_local_spool()`; no OCR/ZXing import or
  network dependency was added.

## Files added for 2B

- `src/label_inspection/station/spool.py`
- `src/label_inspection/station/spool_models.py`
- `tests/test_phase2_spool.py`
- `tasks/phase2_checkpoint_2b_report.md`

## Files updated for 2B

- `.env.example`
- `src/label_inspection/app.py`
- `src/label_inspection/config.py`
- `src/label_inspection/pipeline/types.py`
- `src/label_inspection/station/__init__.py`
- `src/label_inspection/station/preparation.py`
- `tests/test_config_wiring.py`
- `tests/test_phase2_boundaries.py`
- Phase 2 technical design, plan, todo, and historical 2A report.

## Test evidence

TDD RED evidence observed:

- Initial spool tests failed collection because `station.spool` did not exist.
- Delivery/recovery tests failed collection because `SpoolStateError` did not
  exist.
- Backpressure/config tests failed collection because capacity APIs and config
  fields did not exist.
- Local spool factory test failed collection because `build_local_spool` did
  not exist.
- Orientation provenance test failed with missing `orientation_degrees`.

Observed GREEN evidence:

```text
Focused 2A + 2B regression: 40 passed, 1 Windows symlink skip
Config/boundary/spool regression after wiring: 43 passed, 1 Windows symlink skip
Review-fix regression: 46 passed, 1 Windows symlink skip
Full relevant non-runtime suite: 189 passed, 1 skipped, 3 deselected
Three durability/capacity probes: 3 passed
compileall: passed
git diff --check: passed with line-ending warnings only
```

The symlink test was skipped because this Windows account lacks symlink-create
privilege (`WinError 1314`). The containment code and non-symlink traversal
tests passed locally; the symlink test must run on GX10/Linux before a runtime
security claim.

The three explicit probes covered:

1. Failed atomic directory rename never exposes a final dispatchable event and
   preserves the incomplete temp directory.
2. Recovery detects a modified crop by checksum and preserves valid, corrupt,
   and partial evidence.
3. Exact projected byte overflow rejects the event before final or temp path
   creation.

## Review findings resolved

- Split the original oversized spool module into filesystem orchestration and
  typed spool models/errors.
- Manifest filenames now reject both slash styles independent of host OS.
- Frozen job now records the orientation normalization applied by station.
- No new dependency, secret, automatic cleanup, network call, OCR, barcode, or
  business mapping was introduced.

## Not runtime verified

- Actual process kill/power-loss/fsync behavior on the GX10 filesystem.
- Linux symlink-escape regression.
- Spool operation under sustained concurrent triggers/dispatcher activity.
- Production spool thresholds and disk sizing on GX10.
- MinIO upload, RabbitMQ publish confirms, restart resume across network
  outages, resident worker, durable result-before-ACK, retry, or DLQ.

## Known blockers and invariants

- Do not reconstruct the crop from bbox; worker must consume the exact PNG.
- Do not rebuild or mutate `job.json` after local commit.
- Do not dispatch `.tmp_*` or corrupt records.
- Do not delete spool evidence until a retention policy is approved.
- Nvidia P/N / Customer Part Number / Our Part Number remains
  `KNOWN_SEMANTIC_BLOCKER / NEEDS_BUSINESS_CONFIRMATION`; 2B does not change it.

## Checkpoint decision

Checkpoint 2B local acceptance criteria are satisfied by automated evidence.
Human review of durability/path/backpressure policy is required before starting
Checkpoint 2C. No code was committed or pushed.
