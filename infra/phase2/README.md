# Phase 2 local infrastructure

This stack runs only the external delivery dependencies required by the
Phase 2 station/worker flow:

```text
MinIO (S3-compatible artifact store) + RabbitMQ (confirmed job broker)
```

The station and inference worker remain native processes on the GX10. This is
intentional: the worker must use the existing GX10 `.venv` CUDA/PP-OCRv6 and
ZXing runtime. No MediaMTX or camera relay is included.

## Start

```bash
cd ~/Projects/vision-AI
cp infra/phase2/.env.example infra/phase2/.env
nano infra/phase2/.env
```

Replace every `replace-with-*` value and replace `VISION_LABEL_ROI` with the
calibrated normalized ROI. Keep `infra/phase2/.env` private; it is ignored by
Git.

Start the services:

```bash
docker compose \
  --env-file infra/phase2/.env \
  -f infra/phase2/docker-compose.yml \
  up -d
```

`minio-bootstrap` is a one-shot container. An exited status `0` is expected:
it has created the bucket, app user, and restricted policy. The MinIO and
RabbitMQ data volumes persist across `docker compose down`.

Check readiness:

```bash
docker compose --env-file infra/phase2/.env -f infra/phase2/docker-compose.yml ps
curl -fsS http://127.0.0.1:9000/minio/health/live
docker compose --env-file infra/phase2/.env -f infra/phase2/docker-compose.yml \
  exec rabbitmq rabbitmq-diagnostics -q ping
```

The host-side application credentials are:

```text
VISION_MINIO_ENDPOINT=127.0.0.1:9000
VISION_MINIO_ACCESS_KEY=<same value as the Compose app key>
VISION_MINIO_SECRET_KEY=<same value as the Compose app secret>
VISION_RABBITMQ_URL=amqp://<user>:<password>@127.0.0.1:5672/%2F
```

The application declares the durable RabbitMQ exchange/queues itself. The
bootstrap container provisions only the MinIO bucket and app policy; it does
not provision buckets during inspection processing.

## Run the full RTSP flow

Source the same private file in both application terminals:

```bash
cd ~/Projects/vision-AI
source .venv/bin/activate
set -a
source infra/phase2/.env
set +a
```

First verify the direct phone stream:

```bash
python3 scripts/camera_smoke.py \
  --source "$VISION_RTSP_URL" \
  --max-frames 10 \
  --timeout-s 15
```

Terminal 1, start the resident worker and wait for `WORKER_READY`:

```bash
python3 scripts/run_worker.py
```

Terminal 2, start the persistent station loop:

```bash
python3 scripts/run_station.py \
  --source "$VISION_RTSP_URL" \
  --roi "$VISION_LABEL_ROI" \
  --rotate-deg "$VISION_CAMERA_ROTATE_DEG" \
  --triggers 0
```

Wait for `SYSTEM_READY`, press Enter once, and wait for these events:

```text
SYSTEM_READY
LOCAL_COMMIT
ARTIFACTS_READY
JOB_PUBLISHED
RESULT_DURABLE
```

`SYSTEM_READY` means capture is ready. Delivery may initially show
`NOT_CHECKED`; it becomes `READY` after a successful pump cycle or
`DEGRADED` when MinIO/RabbitMQ is unavailable. The first one-event test uses
`--triggers 0` so the station remains alive while its background delivery pump
finishes. After one successful event, use `--triggers 10` for the functional
10-trigger run.

The `event_id` printed by `LOCAL_COMMIT` identifies the local spool record:

```bash
EVENT_ID='paste-the-event-uuid-here'
jq '.event_id, .record_type, .delivery_status' \
  "$VISION_SPOOL_ROOT/$EVENT_ID/state.json"
mc ls --recursive "local/$VISION_ARTIFACT_BUCKET" | grep "$EVENT_ID"
```

For an inference event, MinIO should contain the selected frame, exact label
crop, frozen job, and durable result. Inspect the `result/result.json` path
shown by `mc ls` with `mc cat ... | jq`.

## Stop and reset

Stop containers while preserving data:

```bash
docker compose --env-file infra/phase2/.env -f infra/phase2/docker-compose.yml down
```

`down -v` deletes the MinIO/RabbitMQ volumes and is only for a deliberate
disposable test reset; it removes stored artifacts and broker state.

## Scope and verification status

This is a single-host development/GX10 acceptance stack, not a highly
available production deployment. It binds ports to localhost by default,
uses persistent named volumes, separates MinIO root credentials from the app
user, and leaves the application's private MinIO conditional-write API as a
real-service verification gate. Docker Compose configuration is validated
locally; actual GX10 Docker startup and real MinIO/RabbitMQ behavior must be
run on the target host.
