# ADR-001: Docker Compose cho Phase 2 local/GX10 delivery infrastructure

## Status

Accepted

## Date

2026-08-26

## Context

Phase 2 có hai dependency runtime bên ngoài application process: MinIO cho
immutable artifact delivery và RabbitMQ cho confirmed job delivery. Repository
trước đây chỉ chứa client adapter, không có cách dựng hai service này trên GX10.

Station và inference worker lại phụ thuộc vào runtime GPU/PP-OCRv6/ZXing đã
được cài trong `.venv` native của GX10. Đưa toàn bộ application vào container
sẽ tạo thêm CUDA/Paddle image contract và có thể thay đổi runtime đã được
kiểm chứng.

## Decision

Thêm `infra/phase2/docker-compose.yml` với:

- MinIO single-node và named volume persistent cho object data.
- RabbitMQ management image và named volume persistent cho broker state.
- One-shot `minio-bootstrap` để chờ MinIO, tạo bucket, tạo app user và attach
  policy chỉ cho bucket-location/list và object GET/PUT; không cấp delete.
- Ports bind vào `127.0.0.1` mặc định để không expose delivery services ra LAN.
- Healthcheck RabbitMQ và host-side MinIO liveness check.
- Exact image release tags có thể override có chủ đích qua `.env`.

Station và worker tiếp tục chạy native trên GX10 bằng `.venv`. RabbitMQ
exchange/queue topology tiếp tục do application declare; Compose không tạo
business queue contract riêng.

## Alternatives considered

### Native system packages/services

Không chọn cho acceptance stack vì installation/service names khác nhau theo OS,
khó reproducible và không cung cấp một lifecycle file đi cùng repository.

### Containerize station/worker cùng MinIO/RabbitMQ

Không chọn vì sẽ phải đóng gói CUDA, PaddleOCR và ZXing vào image mới, làm thay
đổi runtime GPU đã được xác nhận trên GX10 và vượt scope Phase 2 foundation.

### Managed/cloud services

Không chọn cho local GX10 flow vì tăng network/credential dependency và không
giải quyết nhu cầu acceptance offline/local.

## Consequences

`docker compose up -d` dựng được delivery dependencies với data persistence và
least-privilege app credentials. `docker compose down` giữ volumes; `down -v`
là thao tác reset có chủ ý và xóa broker/object data.

Đây là single-host development/GX10 acceptance infrastructure, không phải HA
production deployment. Image pull, ARM64 startup, real MinIO conditional PUT,
Rabbit confirms/reconnect và full camera/OCR flow vẫn phải runtime-verify trên
GX10; local Compose parsing không thay thế verification đó.
