# Vision Platform — Database, Backend API & Frontend Design V1

> **Document type:** Technical Design / Implementation Baseline  
> **Scope:** PostgreSQL + Node.js/NestJS Dashboard API + Dashboard Web  
> **Status:** Baseline V1  
> **Principle:** Chỉ chốt các phần đã đủ rõ để triển khai. Các phần chưa có requirement thật được đánh dấu `DEFERRED`, tránh over-engineering.

---

# 1. Mục tiêu

Hệ thống Vision-AI hiện tại cần một nền tảng dữ liệu + backend + dashboard đủ generic để:

- nhận kết quả inspection từ Vision pipeline;
- lưu trạng thái processing, result, timeline và artifact reference;
- hỗ trợ nhiều loại inspection/document/result khác nhau;
- cung cấp Dashboard để theo dõi realtime, history và detail;
- hỗ trợ JWT authentication;
- mở đường cho ERP integration sau này;
- không khóa cứng schema vào một loại shipping label hoặc một bộ field duy nhất.

Luồng tổng quát:

```text
Camera / Input
    ↓
vision-station
    ↓
Capture / Detect / Crop / Quality
    ↓
MinIO + RabbitMQ
    ↓
vision-worker
    ↓
OCR / Barcode / Evidence / Validation
    ↓
PostgreSQL
    ↓
Node.js Dashboard API
    ↓
Dashboard Web
    ↓
ERP sau này
```

---

# 2. Kiến trúc tổng thể

```text
                               ┌────────────────────┐
                               │       CAMERA       │
                               └─────────┬──────────┘
                                         │
                                         ▼
                              ┌──────────────────────┐
                              │    vision-station    │
                              │──────────────────────│
                              │ Capture              │
                              │ Trigger              │
                              │ Detect document      │
                              │ Crop                 │
                              │ Quality              │
                              └───────┬────────┬─────┘
                                      │        │
                                      │        │
                                      ▼        ▼
                                ┌─────────┐  ┌──────────┐
                                │  MinIO  │  │ RabbitMQ │
                                └─────────┘  └────┬─────┘
                                                 │
                                                 ▼
                                      ┌────────────────────┐
                                      │   vision-worker    │
                                      │────────────────────│
                                      │ OCR                │
                                      │ Barcode            │
                                      │ Evidence           │
                                      │ Semantic Mapping*  │
                                      │ Validation*        │
                                      └─────────┬──────────┘
                                                │
                                                │ WRITE
                                                ▼
                                      ┌────────────────────┐
                                      │    PostgreSQL      │
                                      │────────────────────│
                                      │ inspections        │
                                      │ inspection_results │
                                      │ inspection_events  │
                                      │ users              │
                                      │ erp_deliveries*    │
                                      └─────────┬──────────┘
                                                │ READ/WRITE
                                                ▼
                                      ┌────────────────────┐
                                      │ dashboard-api      │
                                      │ Node.js / NestJS   │
                                      └───────┬───────┬────┘
                                              │       │
                                           REST      SSE
                                              │       │
                                              └───┬───┘
                                                  ▼
                                      ┌────────────────────┐
                                      │   Dashboard Web    │
                                      └────────────────────┘
```

`*` Semantic mapping, recognition/profile và ERP có ADR/requirement riêng; không được xem là phần đã chốt hoàn toàn trong tài liệu này.

---

# 3. Nguyên tắc thiết kế chính

1. **PostgreSQL là canonical structured data store.**
2. **MinIO lưu binary artifact**, PostgreSQL chỉ lưu reference.
3. **RabbitMQ dùng cho async transport/job**, không phải source of truth cho Dashboard.
4. **Vision-AI là writer chính của processing data.**
5. **Node.js Dashboard API là backend duy nhất của Frontend.**
6. **Frontend không truy cập trực tiếp PostgreSQL, RabbitMQ hay MinIO credentials.**
7. **DB generic**, không hard-code toàn bộ business field thành column khi vocabulary còn thay đổi.
8. **Technical status tách khỏi business status.**
9. **Không thêm table/service chỉ vì “sau này có thể cần”.**
10. **API list phải nhẹ, detail mới lấy result/event/raw evidence.**

---

# 4. Data ownership

## 4.1 Vision-AI

Vision-AI được phép:

- tạo inspection;
- update processing state;
- lưu result;
- append inspection event;
- lưu artifact reference;
- cập nhật terminal status.

Vision-AI **không tự quản database migration**.

## 4.2 Dashboard API

Node.js/NestJS Dashboard API được phép:

- đọc inspections/results/events;
- quản lý auth/users;
- query dashboard statistics;
- authorize và stream artifact;
- phát SSE notification;
- quản lý các feature Dashboard-owned sau này.

## 4.3 Database migration ownership

Schema migration thuộc Node.js backend repo.

Concept:

```text
dashboard-api/
└── database/
    └── migrations/
        ├── 001_create_inspections
        ├── 002_create_inspection_results
        ├── 003_create_inspection_events
        └── 004_create_users
```

Vision-AI chỉ sử dụng schema thông qua repository/client.

---

# 5. Database naming convention

## 5.1 Table

- `snake_case`
- plural noun

Ví dụ:

```text
inspections
inspection_results
inspection_events
users
erp_deliveries
```

## 5.2 Primary key

Tất cả bảng:

```text
id
```

Ví dụ:

```sql
inspections.id
users.id
inspection_results.id
```

Không dùng:

```text
inspection_id làm PK của inspections
user_id làm PK của users
```

## 5.3 Foreign key

Format:

```text
<entity>_id
```

Ví dụ:

```text
inspection_results.inspection_id → inspections.id
inspection_events.inspection_id  → inspections.id
erp_deliveries.inspection_id     → inspections.id
```

## 5.4 Column

- `snake_case`

Ví dụ:

```text
processing_status
business_status
triggered_at
workflow_version
```

## 5.5 JSON key

Dùng `snake_case` để thống nhất với API/DB:

```json
{
  "source_type": "CAMERA",
  "frame_id": "18231"
}
```

---

# 6. Data type convention

## UUID

Primary key/FK:

```sql
UUID
```

Khuyến nghị application tạo UUID trước khi insert nếu ID cần xuyên suốt pipeline.

## Timestamp

Mọi timestamp persistent:

```sql
TIMESTAMPTZ
```

Lưu UTC.

API trả ISO-8601:

```text
2026-09-04T06:30:15.123Z
```

## JSONB

Dùng cho:

- dữ liệu thay đổi theo result type;
- raw evidence;
- metadata;
- source;
- artifacts;
- metrics;
- validation detail.

Không dùng JSONB cho các field thường xuyên:

- filter;
- join;
- sort;
- aggregate;
- status;
- timestamp.

---

# 7. Status model

## 7.1 Processing Status

Recommended V1:

```text
PENDING
PROCESSING
COMPLETED
ERROR
```

| Status | Meaning |
|---|---|
| `PENDING` | Inspection đã được tạo nhưng chưa xử lý xong |
| `PROCESSING` | Đang chạy pipeline |
| `COMPLETED` | Technical processing hoàn thành |
| `ERROR` | Technical processing terminal error |

Quy tắc:

```text
processing_status = ERROR
business_status = NULL
```

## 7.2 Business Status

```text
PASS
REVIEW
FAIL
```

| Status | Meaning |
|---|---|
| `PASS` | Thỏa business rules được phép áp dụng |
| `REVIEW` | Cần human/business review hoặc có uncertainty |
| `FAIL` | Có business violation đã xác nhận đủ rõ |
| `NULL` | Chưa có decision hoặc technical ERROR |

Không đưa `ERROR` vào business status.

## 7.3 Stage Status

Trong `stage_states`:

```text
NOT_STARTED
RUNNING
COMPLETED
FAILED
SKIPPED
NOT_RUN
```

`current_stage` nếu cần hiển thị ở UI nên derive từ `stage_states`, không là source of truth riêng.

---

# 8. Core tables

---

# 8.1 `inspections`

## Purpose

Một row = một lần inspection / correlation root.

Không khóa cứng:

```text
1 inspection = 1 shipping label
```

Một inspection có thể có nhiều result.

## Columns

| Column | Type | Nullable | Nội dung |
|---|---|---:|---|
| `id` | `UUID` | No | Primary key / correlation ID |
| `inspection_type` | `VARCHAR(64)` | No | Loại workflow/inspection |
| `workflow_version` | `VARCHAR(64)` | No | Version pipeline/workflow |
| `processing_status` | `VARCHAR(32)` | No | `PENDING/PROCESSING/COMPLETED/ERROR` |
| `business_status` | `VARCHAR(32)` | Yes | `PASS/REVIEW/FAIL`, null nếu chưa có/ERROR |
| `stage_states` | `JSONB` | No | Snapshot các stage |
| `source` | `JSONB` | No | Input source/camera/file/replay |
| `summary` | `JSONB` | No | Projection nhỏ cho list/dashboard |
| `artifacts` | `JSONB` | No | Reference tới MinIO |
| `metadata` | `JSONB` | No | Context phụ |
| `triggered_at` | `TIMESTAMPTZ` | No | Thời điểm bắt đầu inspection |
| `completed_at` | `TIMESTAMPTZ` | Yes | Thời điểm terminal |
| `duration_ms` | `INTEGER` | Yes | Tổng thời gian processing |
| `error_code` | `VARCHAR(128)` | Yes | Technical error code |
| `error_message` | `TEXT` | Yes | Technical error message |
| `created_at` | `TIMESTAMPTZ` | No | Created time |
| `updated_at` | `TIMESTAMPTZ` | No | Last updated |

### Recommended defaults

```text
processing_status = PENDING
business_status   = NULL
stage_states      = {}
source            = {}
summary           = {}
artifacts         = {}
metadata          = {}
```

### `source` example

```json
{
  "type": "CAMERA",
  "protocol": "RTSP",
  "source_code": "CAM-01",
  "frame": {
    "frame_id": "18231",
    "captured_at": "2026-09-04T06:30:01Z",
    "width": 1920,
    "height": 1080
  }
}
```

Không lưu secret như RTSP password.

### `summary` example

```json
{
  "document_count": 1,
  "result_count": 1,
  "primary_result_type": "DOCUMENT",
  "display_fields": {
    "barcode": "123456789"
  }
}
```

`summary` chỉ dùng cho list/dashboard, không phải canonical raw result.

### `artifacts` example

```json
{
  "selected_frame": {
    "bucket": "vision",
    "key": "inspections/<inspection_id>/selected-frame.jpg",
    "content_type": "image/jpeg"
  },
  "documents": [
    {
      "index": 0,
      "bucket": "vision",
      "key": "inspections/<inspection_id>/documents/0.jpg",
      "content_type": "image/jpeg"
    }
  ]
}
```

### `metadata` example

```json
{
  "station_code": "GX10-01",
  "line_code": "LINE-01",
  "shift": "A",
  "trigger": {
    "type": "MANUAL"
  },
  "runtime": {
    "station_version": "1.0.0",
    "worker_version": "1.0.0"
  }
}
```

---

# 8.2 `inspection_results`

## Purpose

Một inspection có thể có N result.

```text
inspection
   ├── result #0
   ├── result #1
   └── result #N
```

## Columns

| Column | Type | Nullable | Nội dung |
|---|---|---:|---|
| `id` | `UUID` | No | PK |
| `inspection_id` | `UUID` | No | FK → `inspections.id` |
| `result_type` | `VARCHAR(64)` | No | Loại result |
| `result_index` | `SMALLINT` | No | Index cùng type |
| `schema_version` | `VARCHAR(32)` | No | Version của JSON result contract |
| `status` | `VARCHAR(32)` | No | Result-level status |
| `data` | `JSONB` | No | Structured/canonical result |
| `raw_data` | `JSONB` | No | Raw OCR/barcode/evidence |
| `raw_artifact_ref` | `JSONB` | Yes | Reference nếu raw payload lớn |
| `validation` | `JSONB` | No | Validation/business reasons |
| `metrics` | `JSONB` | No | Latency/confidence/resource metrics |
| `metadata` | `JSONB` | No | Provenance/context |
| `produced_at` | `TIMESTAMPTZ` | No | Thời điểm tạo result |
| `created_at` | `TIMESTAMPTZ` | No | Created |
| `updated_at` | `TIMESTAMPTZ` | No | Updated |

Constraint:

```sql
UNIQUE (inspection_id, result_type, result_index)
```

### Possible `result_type`

```text
DOCUMENT
SHIPPING_LABEL
BARCODE_SET
VISUAL_INSPECTION
WEIGHT_CHECK
```

Không dùng DB enum quá sớm; application dùng typed constants.

### `data` example

Khi chưa có business-approved semantic mapping:

```json
{
  "extracted": {}
}
```

Khi future approved mapping tồn tại:

```json
{
  "extracted": {
    "quantity": {
      "value": 20,
      "source_ref": "obs-123"
    }
  }
}
```

### `raw_data` example

```json
{
  "ocr": {
    "lines": [
      {
        "id": "ocr-1",
        "text": "NVIDIA P/N",
        "confidence": 0.98,
        "polygon": [[100,20],[200,20],[200,50],[100,50]]
      }
    ]
  },
  "barcodes": [
    {
      "symbology": "CODE128",
      "value": "123456789"
    }
  ],
  "evidence": [
    {
      "source_label": "NVIDIA P/N",
      "raw_value": "ABC-001",
      "semantic_status": "UNMAPPED",
      "canonical_field": null
    }
  ]
}
```

### `validation` example

```json
{
  "business_status": "REVIEW",
  "reasons": [
    {
      "code": "UNMAPPED_SEMANTIC",
      "message": "Business semantic mapping has not been approved."
    }
  ]
}
```

### `metrics` example

```json
{
  "total_ms": 827,
  "ocr_ms": 510,
  "barcode_ms": 96,
  "quality_ms": 32
}
```

---

# 8.3 `inspection_events`

## Purpose

`inspections` = current state.  
`inspection_events` = timeline / chuyện đã xảy ra.

## Columns

| Column | Type | Nullable | Nội dung |
|---|---|---:|---|
| `id` | `UUID` | No | Unique event ID |
| `inspection_id` | `UUID` | No | FK → `inspections.id` |
| `event_type` | `VARCHAR(64)` | No | Loại event |
| `stage` | `VARCHAR(64)` | Yes | Stage liên quan |
| `source` | `VARCHAR(64)` | No | Component phát event |
| `status` | `VARCHAR(32)` | Yes | Optional stage/event status |
| `occurred_at` | `TIMESTAMPTZ` | No | Event time |
| `duration_ms` | `INTEGER` | Yes | Duration nếu có |
| `data` | `JSONB` | No | Event-specific payload |
| `created_at` | `TIMESTAMPTZ` | No | Insert time |

Possible source:

```text
vision-station
vision-worker
dashboard-api
erp-adapter
```

Possible event types:

```text
INSPECTION_CREATED
TRIGGER_RECEIVED
FRAME_SELECTED
DOCUMENT_DETECTED
QUALITY_COMPLETED
JOB_PUBLISHED
WORKER_STARTED
OCR_STARTED
OCR_COMPLETED
BARCODE_STARTED
BARCODE_COMPLETED
VALIDATION_COMPLETED
PROCESSING_COMPLETED
PROCESSING_ERROR
```

---

# 8.4 `users`

## Purpose

Dashboard authentication/authorization.

## Columns

| Column | Type | Nullable | Nội dung |
|---|---|---:|---|
| `id` | `UUID` | No | PK |
| `username` | `VARCHAR(100)` | No | Login username |
| `email` | `VARCHAR(255)` | Yes | Email |
| `password_hash` | `TEXT` | No | Password hash |
| `full_name` | `VARCHAR(255)` | Yes | Display name |
| `role` | `VARCHAR(32)` | No | Role V1 |
| `status` | `VARCHAR(32)` | No | Account status |
| `refresh_token_hash` | `TEXT` | Yes | Refresh token hash nếu dùng |
| `token_version` | `INTEGER` | No | Force invalidate token |
| `last_login_at` | `TIMESTAMPTZ` | Yes | Last login |
| `password_changed_at` | `TIMESTAMPTZ` | Yes | Password change |
| `created_at` | `TIMESTAMPTZ` | No | Created |
| `updated_at` | `TIMESTAMPTZ` | No | Updated |

Role V1:

```text
ADMIN
OPERATOR
ENGINEER
VIEWER
```

Status:

```text
ACTIVE
DISABLED
LOCKED
```

Không lưu plain password.

Không tạo `roles`, `permissions`, `sessions` table V1 nếu chưa có requirement.

---

# 8.5 `erp_deliveries` — DEFERRED

Chỉ tạo khi ERP integration thực sự bắt đầu.

| Column | Type | Nullable | Nội dung |
|---|---|---:|---|
| `id` | `UUID` | No | PK |
| `inspection_id` | `UUID` | No | FK |
| `target_system` | `VARCHAR(64)` | No | ERP destination |
| `status` | `VARCHAR(32)` | No | Delivery state |
| `payload` | `JSONB` | No | Payload gửi ERP |
| `response` | `JSONB` | Yes | Sanitized response |
| `idempotency_key` | `VARCHAR(255)` | No | Prevent duplicate |
| `attempt_count` | `INTEGER` | No | Retry count |
| `http_status` | `INTEGER` | Yes | Last HTTP status |
| `error_code` | `VARCHAR(128)` | Yes | Delivery error code |
| `error_message` | `TEXT` | Yes | Last error |
| `next_retry_at` | `TIMESTAMPTZ` | Yes | Retry time |
| `delivered_at` | `TIMESTAMPTZ` | Yes | Success time |
| `metadata` | `JSONB` | No | Extra context |
| `created_at` | `TIMESTAMPTZ` | No | Created |
| `updated_at` | `TIMESTAMPTZ` | No | Updated |

Possible status:

```text
PENDING
SENDING
DELIVERED
RETRYING
FAILED
```

ERP failure không được làm Vision processing thành technical failure.

---

# 9. Deferred tables

Không tạo mặc định trong V1:

```text
stations
workers
roles
permissions
sessions
profiles
profile_versions
canonical_fields
inspection_reviews
```

Lý do:

- chưa có nhu cầu registry thực tế;
- tránh over-engineering;
- chưa chốt lifecycle tương ứng.

---

# 10. Relationships

```text
inspections
  ├── 1:N inspection_results
  ├── 1:N inspection_events
  └── 1:N erp_deliveries   # future

users
  └── independent auth domain
```

Recommended delete behavior:

```text
ON DELETE RESTRICT
```

Không cascade-delete production audit history mặc định.

---

# 11. DDL baseline

```sql
CREATE TABLE inspections (
    id UUID PRIMARY KEY,
    inspection_type VARCHAR(64) NOT NULL,
    workflow_version VARCHAR(64) NOT NULL,

    processing_status VARCHAR(32) NOT NULL,
    business_status VARCHAR(32),

    stage_states JSONB NOT NULL DEFAULT '{}'::jsonb,
    source JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifacts JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    triggered_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,

    error_code VARCHAR(128),
    error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE inspection_results (
    id UUID PRIMARY KEY,
    inspection_id UUID NOT NULL REFERENCES inspections(id),

    result_type VARCHAR(64) NOT NULL,
    result_index SMALLINT NOT NULL DEFAULT 0,
    schema_version VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,

    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_artifact_ref JSONB,
    validation JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    produced_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_inspection_result
        UNIQUE (inspection_id, result_type, result_index)
);

CREATE TABLE inspection_events (
    id UUID PRIMARY KEY,
    inspection_id UUID NOT NULL REFERENCES inspections(id),

    event_type VARCHAR(64) NOT NULL,
    stage VARCHAR(64),
    source VARCHAR(64) NOT NULL,
    status VARCHAR(32),

    occurred_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE users (
    id UUID PRIMARY KEY,

    username VARCHAR(100) NOT NULL UNIQUE,
    email VARCHAR(255),
    password_hash TEXT NOT NULL,
    full_name VARCHAR(255),

    role VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,

    refresh_token_hash TEXT,
    token_version INTEGER NOT NULL DEFAULT 0,

    last_login_at TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX uq_users_email
    ON users(email)
    WHERE email IS NOT NULL;
```

---

# 12. Index baseline

```sql
CREATE INDEX idx_inspections_triggered_at
    ON inspections (triggered_at DESC, id DESC);

CREATE INDEX idx_inspections_processing_status_triggered_at
    ON inspections (processing_status, triggered_at DESC);

CREATE INDEX idx_inspections_business_status_triggered_at
    ON inspections (business_status, triggered_at DESC)
    WHERE business_status IS NOT NULL;

CREATE INDEX idx_inspections_type_triggered_at
    ON inspections (inspection_type, triggered_at DESC);

CREATE INDEX idx_results_inspection_id
    ON inspection_results (inspection_id);

CREATE INDEX idx_events_inspection_time
    ON inspection_events (inspection_id, occurred_at ASC, id ASC);
```

Không tạo GIN index cho mọi JSONB.

---

# 13. Transaction boundary

## Inspection creation

```text
BEGIN
  INSERT inspections
  INSERT inspection_events
COMMIT
```

## Terminal processing

```text
BEGIN
  INSERT/UPSERT inspection_results
  UPDATE inspections
  INSERT inspection_events
COMMIT
```

Mục tiêu:

Không để:

```text
inspection = COMPLETED
```

nhưng result chưa được lưu.

---

# 14. Idempotency

Async worker có thể bị redelivery.

Core constraint:

```sql
UNIQUE (inspection_id, result_type, result_index)
```

Repository có thể dùng:

```text
INSERT ... ON CONFLICT ...
```

theo semantics đã chốt.

Không tạo duplicate result vì RabbitMQ redelivery.

---

# 15. Query strategy và N+1

## List

`GET /inspections` chỉ query projection từ `inspections`.

Không:

```text
for each inspection:
    query results
    query events
```

## Detail

Có thể dùng số query cố định:

```text
1. inspection
2. results
3. events
4. ERP delivery nếu feature tồn tại
```

3–4 query cố định không phải N+1.

---

# 16. Pagination

Dùng keyset/cursor:

```text
(triggered_at, id)
```

SQL:

```sql
ORDER BY triggered_at DESC, id DESC
```

Không dùng deep `OFFSET` cho history lớn.

---

# 17. Node.js / NestJS backend architecture

## Responsibilities

Dashboard API chịu trách nhiệm:

- JWT auth;
- user management;
- inspection queries;
- results;
- timeline/events;
- dashboard aggregate;
- artifact gateway;
- SSE realtime;
- ERP/admin integration sau này.

Không chạy:

- OCR;
- detector;
- barcode decoding;
- camera capture;
- vision inference.

## Suggested module structure

```text
src/
├── app.module.ts
├── auth/
├── users/
├── inspections/
├── results/
├── events/
├── artifacts/
├── realtime/
├── dashboard/
├── database/
└── common/
```

Detailed:

```text
auth/
├── auth.module.ts
├── auth.controller.ts
├── auth.service.ts
├── guards/
├── strategies/
└── dto/

inspections/
├── inspections.module.ts
├── inspections.controller.ts
├── inspections.service.ts
├── inspections.repository.ts
└── dto/
```

ORM cụ thể là `DEFERRED`.

---

# 18. Backend layering

```text
Controller
    ↓
Service
    ↓
Repository
    ↓
PostgreSQL
```

Controller:

- parse input;
- validate DTO;
- auth;
- response.

Service:

- orchestration/use-case logic.

Repository:

- SQL/query;
- projection;
- transaction.

Không gọi DB trực tiếp từ controller.

---

# 19. TypeScript coding convention

## Class

```text
PascalCase
```

Ví dụ:

```ts
InspectionService
InspectionRepository
ListInspectionsQueryDto
```

## Variable/function

```text
camelCase
```

Ví dụ:

```ts
inspectionId
getInspectionById()
```

## Constants

```text
UPPER_SNAKE_CASE
```

hoặc typed `as const`.

## Database/API field

```text
snake_case
```

Nếu TypeScript domain dùng camelCase thì mapping phải nằm ở boundary rõ ràng, không map rải rác.

---

# 20. DTO convention

Public HTTP input phải validate bằng DTO/schema.

Ví dụ:

```ts
class ListInspectionsQueryDto {
  cursor?: string;
  limit?: number;
  processing_status?: string;
  business_status?: string;
  inspection_type?: string;
  from?: string;
  to?: string;
}
```

Không nhận arbitrary object và truyền thẳng xuống query.

---

# 21. Error response convention

Recommended:

```json
{
  "error": {
    "code": "INSPECTION_NOT_FOUND",
    "message": "Inspection not found",
    "details": null,
    "request_id": "..."
  }
}
```

Không expose:

- stack trace production;
- DB connection string;
- MinIO secret;
- local file path;
- JWT/refresh token.

---

# 22. Logging convention

Structured log.

Recommended fields:

```text
request_id
inspection_id
user_id
module
operation
duration_ms
error_code
```

Không log:

```text
password
JWT
refresh token
camera password
MinIO secret
```

---

# 23. API convention

Base path:

```text
/api/v1
```

JSON:

```text
application/json
```

Time:

```text
ISO-8601 UTC
```

IDs:

```text
UUID strings
```

---

# 24. Auth API

## POST `/api/v1/auth/login`

Request:

```json
{
  "username": "operator01",
  "password": "..."
}
```

Response:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_in": 900,
  "user": {
    "id": "uuid",
    "username": "operator01",
    "full_name": "Operator 01",
    "role": "OPERATOR"
  }
}
```

## POST `/api/v1/auth/refresh`

Refresh access token.

## POST `/api/v1/auth/logout`

Invalidate refresh token / bump token version tùy implementation.

## GET `/api/v1/auth/me`

Response:

```json
{
  "id": "uuid",
  "username": "operator01",
  "email": null,
  "full_name": "Operator 01",
  "role": "OPERATOR",
  "status": "ACTIVE"
}
```

---

# 25. Inspections API

## GET `/api/v1/inspections`

Dùng cho:

- recent inspections;
- history;
- filters;
- pagination.

Query:

```text
limit
cursor
processing_status
business_status
inspection_type
from
to
```

Example response:

```json
{
  "items": [
    {
      "id": "uuid",
      "inspection_type": "DOCUMENT_INSPECTION",
      "workflow_version": "v1",
      "processing_status": "COMPLETED",
      "business_status": "REVIEW",
      "summary": {},
      "triggered_at": "2026-09-04T06:30:01Z",
      "completed_at": "2026-09-04T06:30:02Z",
      "duration_ms": 900
    }
  ],
  "next_cursor": "..."
}
```

Không trả full result/event/raw OCR ở list.

## GET `/api/v1/inspections/:id`

Inspection snapshot.

## GET `/api/v1/inspections/:id/results`

Result list/detail.

## GET `/api/v1/inspections/:id/events`

Timeline.

---

# 26. Artifact API

Frontend không được lấy MinIO credentials.

Recommended:

```text
GET /api/v1/inspections/:inspection_id/artifacts/:artifact_key
```

Flow:

```text
JWT
  ↓
authorize
  ↓
resolve artifact
  ↓
stream/proxy MinIO
```

Không để browser dùng internal MinIO endpoint như:

```text
127.0.0.1:9000
```

V1 ưu tiên backend gateway/stream.

---

# 27. Dashboard aggregate API

Recommended:

```text
GET /api/v1/dashboard/overview
```

Example:

```json
{
  "window": {
    "from": "...",
    "to": "..."
  },
  "totals": {
    "inspections": 1000,
    "completed": 960,
    "errors": 40
  },
  "business": {
    "pass": 500,
    "review": 430,
    "fail": 30
  },
  "performance": {
    "avg_duration_ms": 850,
    "p95_duration_ms": 1300
  }
}
```

Frontend không nên tải hàng nghìn inspection rồi tự aggregate.

---

# 28. Realtime — SSE

V1 ưu tiên SSE thay vì WebSocket vì dashboard chủ yếu server → client.

Endpoint concept:

```text
GET /api/v1/realtime/inspections
```

Event:

```text
event: inspection.updated
data: {"inspection_id":"uuid","processing_status":"PROCESSING"}
```

Nguyên tắc:

```text
SSE event
    ↓
invalidate/refetch
    ↓
REST remains source of truth
```

Nếu SSE disconnect:

```text
reconnect
→ REST snapshot
→ resume SSE
```

Không stream live camera video qua SSE.

---

# 29. Frontend architecture

```text
Dashboard Web
      │
      ├── REST
      └── SSE
             │
             ▼
       Dashboard API
          │      │
          │      └── MinIO
          ▼
      PostgreSQL
```

Frontend không truy cập:

```text
PostgreSQL
RabbitMQ
MinIO secret
```

trực tiếp.

---

# 30. Frontend pages

## Login

- username/password;
- invalid credentials;
- token refresh;
- logout;
- route guard.

## Overview

Hiển thị:

```text
Total inspections
Processing
Completed
Technical Errors
PASS
REVIEW
FAIL
Average latency
P95 latency
```

## Live Monitor

Ví dụ:

```text
Inspection ID
Source
Started At

Capture      COMPLETED
Detection    COMPLETED
Quality      COMPLETED
OCR          RUNNING
Barcode      COMPLETED
Validation   NOT_STARTED
```

Dùng REST snapshot + SSE notifications.

## Inspections

Columns:

```text
ID
Inspection Type
Triggered At
Processing Status
Business Status
Summary
Duration
```

Filters:

```text
time range
processing status
business status
inspection type
```

## History

Có thể là route UI riêng nhưng dùng cùng:

```text
GET /api/v1/inspections
```

với `from/to/cursor`.

Không cần backend `/history` riêng nếu semantics giống inspection query.

## Inspection Detail

Sections:

```text
General
Current Status
Stage Status
Source
Artifacts
Results
Raw Evidence
Validation
Metrics
Timeline
ERP Delivery*
```

## Timeline

Render từ `inspection_events`.

---

# 31. Frontend coding convention

Recommended:

```text
TypeScript strict mode
component/function names: PascalCase
hooks/functions/variables: camelCase
API DTO types: explicit
no any at API boundary
```

Suggested structure:

```text
src/
├── app/
├── pages/
├── features/
│   ├── auth/
│   ├── inspections/
│   ├── dashboard/
│   └── realtime/
├── components/
├── api/
├── hooks/
├── types/
└── utils/
```

Không gọi API trực tiếp rải trong UI component.

Tách:

```text
API client
→ query/cache layer
→ feature hook
→ UI component
```

---

# 32. API security

Backend phải:

- verify JWT;
- authorize role;
- validate DTO;
- parameterized query;
- hash password;
- sanitize errors;
- validate artifact ownership;
- rate-limit auth nếu production cần.

Không gửi ra FE:

```text
password_hash
refresh_token_hash
MinIO keys
internal stack trace
```

---

# 33. Database security

Recommended:

```text
migration role:
    schema DDL

vision role:
    inspection/result/event DML

dashboard role:
    read inspection/result/event
    users DML
```

Đây là `RECOMMENDED`, chưa bắt buộc nếu deployment hiện tại nhỏ.

---

# 34. Error semantics

Technical error:

```json
{
  "processing_status": "ERROR",
  "business_status": null,
  "error_code": "OCR_RUNTIME_ERROR",
  "error_message": "OCR execution failed"
}
```

Business uncertainty:

```json
{
  "processing_status": "COMPLETED",
  "business_status": "REVIEW"
}
```

Không:

```text
OCR crash → REVIEW
```

Không:

```text
unknown semantic → ERROR
```

---

# 35. Artifact naming convention

Recommended:

```text
inspections/{inspection_id}/selected-frame.jpg
inspections/{inspection_id}/documents/0.jpg
inspections/{inspection_id}/documents/1.jpg
```

DB lưu object key/reference.

---

# 36. API response size convention

List endpoint không trả:

- raw OCR;
- full timeline;
- full artifact metadata;
- full evidence payload.

Phân tách:

```text
GET /inspections
→ light list

GET /inspections/:id
→ snapshot

GET /inspections/:id/results
→ result

GET /inspections/:id/events
→ timeline
```

---

# 37. Observability

Backend metrics:

```text
HTTP request count
HTTP latency
5xx count
DB query latency
SSE connected clients
artifact streaming errors
```

Inspection metrics:

```text
processing duration
OCR duration
barcode duration
error rate
PASS/REVIEW/FAIL counts
```

Correlation fields:

```text
inspection_id
request_id
```

---

# 38. API/versioning convention

HTTP:

```text
/api/v1
```

Vision/data contracts:

```text
workflow_version
schema_version
```

Không dùng một version cho mọi thứ.

- API version = HTTP contract.
- workflow version = Vision pipeline.
- schema version = result JSON contract.

---

# 39. Coding principles

1. Explicit over magic.
2. Fail closed với business certainty.
3. Technical state tách business state.
4. Raw evidence không bị overwrite bởi semantic/canonical mapping.
5. DB là source of truth cho structured state.
6. SSE là notification, không phải state store.
7. Image/file ở object storage.
8. Không N+1.
9. Không hard-code future business vocabulary.
10. Không thêm table/service chỉ vì “có thể sẽ cần”.
11. List API nhẹ; detail mới tải dữ liệu sâu.
12. Migration ownership rõ ràng.
13. Không để FE biết infrastructure credential.
14. Không coi summary JSON là canonical result.

---

# 40. Naming summary

| Context | Convention | Example |
|---|---|---|
| DB table | `snake_case`, plural | `inspection_results` |
| DB column | `snake_case` | `processing_status` |
| PK | `id` | `inspections.id` |
| FK | `<entity>_id` | `inspection_id` |
| API path | plural noun | `/inspections` |
| JSON field | `snake_case` | `business_status` |
| TS class | `PascalCase` | `InspectionService` |
| TS variable | `camelCase` | `inspectionId` |
| Event type | `UPPER_SNAKE_CASE` | `OCR_COMPLETED` |
| Status | `UPPER_SNAKE_CASE` | `PROCESSING` |

---

# 41. Chưa chốt / Deferred

Các phần sau chưa coi là final:

- ORM: Prisma / TypeORM / Drizzle / raw SQL;
- profile persistence;
- recognition engine;
- final business vocabulary;
- human review lifecycle;
- final ERP contract;
- station registry;
- worker registry;
- advanced RBAC;
- canonical field registry;
- profile approval workflow;
- multi-document business policy;
- exact retention policy;
- JSONB indexes theo production query.

Khi có requirement thật, bổ sung ADR/migration riêng.

---

# 42. V1 implementation order

```text
1. PostgreSQL migrations
   ├── inspections
   ├── inspection_results
   ├── inspection_events
   └── users

2. Vision repository
   ├── create inspection
   ├── update processing state
   ├── save result
   └── append event

3. Dashboard API
   ├── auth
   ├── inspections list/detail
   ├── results
   ├── events
   ├── artifacts
   └── dashboard aggregate

4. Dashboard FE
   ├── login
   ├── overview
   ├── inspections/history
   ├── inspection detail
   └── timeline

5. SSE realtime

6. ERP integration later
```

---

# 43. Final V1 baseline

```text
Camera
  │
  ▼
vision-station
  │
  ├─────────────► MinIO
  │
  ▼
RabbitMQ
  │
  ▼
vision-worker
  │
  ▼
PostgreSQL
  │
  ├── inspections
  ├── inspection_results
  ├── inspection_events
  └── users
  │
  ▼
NestJS Dashboard API
  │
  ├── REST
  ├── SSE
  ├── JWT
  └── Artifact Gateway
  │
  ▼
Dashboard Web
```

---

# 44. Final decisions summary

## CHỐT V1

- Một shared PostgreSQL.
- Vision và Node.js dùng cùng database.
- Vision là processing writer chính.
- Node.js quản schema/migration.
- PostgreSQL giữ structured state.
- MinIO giữ image/artifact.
- RabbitMQ giữ async transport.
- `id` là PK chuẩn.
- FK là `<entity>_id`.
- Core tables:
  - `inspections`
  - `inspection_results`
  - `inspection_events`
  - `users`
- ERP table chỉ thêm khi triển khai ERP.
- Không tạo station/worker/profile registry V1.
- Result schema generic + JSONB.
- Field cần filter/sort/join là column.
- Timestamp dùng `TIMESTAMPTZ`.
- History dùng cursor pagination.
- Node API tránh N+1.
- REST là state retrieval.
- SSE là realtime notification.
- FE không truy cập DB/RabbitMQ/MinIO credential trực tiếp.
- Technical `ERROR` tách khỏi `PASS/REVIEW/FAIL`.
- Không publish canonical semantic field khi business mapping chưa được approve.

## DEFERRED

- ORM.
- Profile persistence.
- Recognition engine.
- Human review table/workflow.
- ERP contract cuối.
- Station/worker registry.
- Advanced RBAC.
- JSONB search/index tuning theo production query.
