# Phase 2 Operations Runbook

This runbook describes the deployment contract for the Phase 2 worker. It is
an operational procedure, not evidence that the production services have been
deployed in this workspace. Transport dependencies are controlled by exact
versions in the `phase2` optional dependency group in `pyproject.toml`.

## Provisioning

An administrator provisions the configured artifact bucket before starting the
worker service. Provisioning may call `ensure_bucket` once during installation.
The worker validates the bucket at startup. The station keeps capture and local
spool startup independent from MinIO; its delivery pump validates/connects
lazily when a pending record needs delivery. Runtime services must not create
buckets while processing an event.

The runtime identity needs only the bucket/object operations required by the
configured workflow: bucket existence validation, object HEAD/GET for worker
inputs, and create-only object writes for station/result artifacts. It must not
have broad bucket-admin permissions in the steady-state service role.

The station's MinIO adapter intentionally uses the pinned client's private
`_put_object` primitive because the public `put_object` API cannot express an
atomic `If-None-Match: *` create-only write. A HEAD-then-PUT fallback would
reintroduce a race between idempotent retry and conflicting content. This
compatibility surface remains NOT RUNTIME VERIFIED until tested against the
deployed MinIO server.

## Worker supervision

Install `ops/systemd/vision-inference-worker.service` with the repository
paths adjusted for the host, then run:

```text
sudo systemctl daemon-reload
sudo systemctl enable --now vision-inference-worker.service
sudo systemctl status vision-inference-worker.service
```

The worker uses the shared `RetryingWorkerMessageHandler` used by tests. If
RabbitMQ consumption loses its connection, the process emits
`BROKER_CONNECTION_LOST` and exits with status 1. systemd then restarts it
according to `Restart=on-failure`. Confirm recovery with
`journalctl -u vision-inference-worker.service` and the broker connection
metrics.

## Timing and observability

`stage_timings` and worker lifecycle logs keep download, checksum verification,
image decode, OCR, barcode, extraction, validation, and total timings as
separate fields. `image_download_ms` covers artifact retrieval only;
`checksum_verify_ms` covers digest verification only; `image_decode_ms` covers
lossless crop decode only. Model load and warmup happen before READY and are
not included in per-inspection OCR timing.

## Integration gate

Local tests use the in-memory store and fake transport. A real deployment must
still verify the MinIO and RabbitMQ adapters with the actual service identity,
pre-provisioned bucket, TLS/authentication settings, and supervisor restart
behavior. The MinIO create-only upload implementation remains an explicit
integration gate until it is exercised against the target service.
