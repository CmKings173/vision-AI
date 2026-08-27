# Phase 2 Infrastructure Runbook

## Scope

`infra/phase2/docker-compose.yml` supplies the two external services required
by the Phase 2 station/worker contract:

```text
station (native GX10 process) -> MinIO -> RabbitMQ -> worker (native GX10 process)
```

It deliberately does not containerize PP-OCRv6, ZXing, station acquisition,
or the Android camera. The GX10 `.venv` remains the owner of the GPU runtime.
There is no MediaMTX component.

## Components

| Component | Container responsibility | Host contract |
|---|---|---|
| MinIO | S3-compatible object store | API `127.0.0.1:9000`, console `127.0.0.1:9001` |
| `minio-bootstrap` | Wait for MinIO, create bucket/app policy/user | One-shot exit `0` is expected |
| RabbitMQ | Durable broker and management UI | AMQP `127.0.0.1:5672`, UI `127.0.0.1:15672` |
| Station/worker | Not containerized | Native Python processes in GX10 `.venv` |

The default ports bind to localhost. Change the bind address only when a
separate host must reach the services, and then apply a firewall policy.

## Credentials and permissions

Copy `infra/phase2/.env.example` to `infra/phase2/.env` and replace all
`replace-with-*` values. Use generated high-entropy values, for example:

```bash
openssl rand -hex 24
```

The MinIO root account is used only by the bootstrap container. The station
and worker use `VISION_MINIO_ACCESS_KEY` / `VISION_MINIO_SECRET_KEY`, which
receive only bucket-location/list and object GET/PUT permissions. Delete is
not granted. RabbitMQ uses the configured application user and vhost.

Never commit `infra/phase2/.env` or put its values in GitHub.

## Lifecycle

```bash
cd ~/Projects/vision-AI
cp infra/phase2/.env.example infra/phase2/.env
nano infra/phase2/.env

docker compose \
  --env-file infra/phase2/.env \
  -f infra/phase2/docker-compose.yml \
  up -d
```

`minio-bootstrap` is idempotent for the default stack. `docker compose down`
preserves named volumes. `docker compose down -v` is destructive and resets
the disposable acceptance environment.

Readiness checks:

```bash
curl -fsS http://127.0.0.1:9000/minio/health/live
docker compose --env-file infra/phase2/.env -f infra/phase2/docker-compose.yml ps
docker compose --env-file infra/phase2/.env -f infra/phase2/docker-compose.yml \
  exec rabbitmq rabbitmq-diagnostics -q ping
```

MinIO readiness is checked by the bootstrap client loop and the host liveness
endpoint. RabbitMQ has a container healthcheck. The application worker still
validates the pre-provisioned bucket before declaring `WORKER_READY`.

## Full RTSP acceptance

Use the same environment file in both terminals:

```bash
set -a
source infra/phase2/.env
set +a
```

The direct Android camera source may be the previously working
`http://10.10.12.13:8080/video` endpoint or a native `rtsp://...` endpoint from
the app. `RTSPCamera` passes either URL to OpenCV. The FixedROI value must be a
calibrated normalized ROI; do not use `0,0,1,1` for the label acceptance.

Run `python3 scripts/camera_smoke.py` first, then start:

```bash
python3 scripts/run_worker.py
```

Wait for `WORKER_READY`. In a second terminal run:

```bash
python3 scripts/run_station.py \
  --source "$VISION_RTSP_URL" \
  --roi "$VISION_LABEL_ROI" \
  --rotate-deg "$VISION_CAMERA_ROTATE_DEG" \
  --triggers 0
```

Press Enter after `SYSTEM_READY`. The expected progression is:

```text
LOCAL_COMMIT (local spool, UUID)
  -> ARTIFACTS_READY (MinIO upload/read-back verified)
  -> JOB_PUBLISHED (Rabbit publisher confirm)
  -> RESULT_DURABLE (worker result persisted/read-back verified, then ACK)
```

For the first one-event flow, leave the persistent station running until
`JOB_PUBLISHED` and `RESULT_DURABLE` appear. Then stop it with Ctrl-C. Run
`--triggers 10` only after this path succeeds.

## Failure interpretation

| Observed state | Meaning |
|---|---|
| `SYSTEM_READY` + `delivery_health=NOT_CHECKED` | Capture works; no delivery attempt has completed yet |
| `LOCAL_ONLY` + `DEGRADED` | Local commit works; MinIO delivery is unavailable or failed |
| `ARTIFACTS_READY` without `JOB_PUBLISHED` | MinIO succeeded; RabbitMQ publish/confirm is pending or failed |
| `JOB_PUBLISHED` without `RESULT_DURABLE` | Worker consumption, OCR/ZXing, result persistence, or ACK path failed |
| `RESULT_DURABLE` | Full one-event flow completed; inspect timings and business result |

## Verification boundary

Local Compose parsing and shell syntax are checked in CI/local validation.
Pulling images, starting containers on GX10, real MinIO conditional PUT,
RabbitMQ confirms/reconnect, camera stream, and PP-OCRv6/ZXing execution are
runtime gates and must be reported as verified only after running them on the
target host.
