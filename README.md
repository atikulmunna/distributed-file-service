# Distributed File Service (Phase 1 MVP)

This repository contains a runnable MVP backend for chunked uploads/downloads based on the simplified spec.

## Implemented
- Upload initialization
- Parallel chunk upload via bounded worker pool
- Retry loop for transient chunk write failures
- Resume support (`missing-chunks`)
- Upload completion validation
- Optional checksum validation (chunk + full file)
- Optional S3 multipart upload flow (`STORAGE_BACKEND=s3`)
- Download streaming with basic HTTP Range support
- Backpressure (`429`) on queue/global/per-upload inflight limits
- Prometheus metrics at `/metrics`
- Structured audit logs (`dfs.audit`) for init/complete/download actions
- Request-latency histogram (`http_request_duration_seconds`) for p95/p99 tracking
- Optional adaptive worker autoscaling with cooldown/hysteresis

Storage defaults to local filesystem, and DB defaults to SQLite.
You can switch to PostgreSQL via `DATABASE_URL` and use S3-compatible backends (`s3` or `r2`).

## Run
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

## Docker Deployment
Run app + PostgreSQL locally with Docker:
```bash
docker compose up --build
```

Service will be available at:
- API: `http://127.0.0.1:8000`
- Metrics: `http://127.0.0.1:8000/metrics`

Notes:
- `docker-compose.yml` overrides `DATABASE_URL` to PostgreSQL inside the Docker network.
- Keep secrets in `.env` (already gitignored).
- To stop:
```bash
docker compose down
```

## Config
Environment variables (defaults in `app/config.py`):
- `DATABASE_URL`
- `STORAGE_BACKEND` (`local`, `s3`, or `r2`)
- `STORAGE_ROOT`
- `S3_BUCKET` (required for `STORAGE_BACKEND=s3`)
- `AWS_REGION`
- `R2_BUCKET` (required for `STORAGE_BACKEND=r2`)
- `R2_ACCOUNT_ID` (required for `STORAGE_BACKEND=r2` unless `R2_ENDPOINT_URL` is set)
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_ENDPOINT_URL` (optional override)
- `API_KEY_MAPPINGS` (format: `apiKey:userId,apiKey2:userId2`)
- `ADMIN_USER_IDS` (comma-separated user IDs allowed on admin endpoints)
- `API_RATE_LIMIT_PER_MINUTE` (`0` disables API-key rate limiting)
- `AUTH_MODE` (`api_key`, `jwt`, `hybrid`)
- `JWT_SECRET`
- `JWT_ALGORITHM` (default `HS256`)
- `JWT_AUDIENCE` (optional)
- `JWT_ISSUER` (optional)
- `TRACING_ENABLED` (`true`/`false`)
- `TRACING_SERVICE_NAME`
- `OTLP_ENDPOINT` (default `localhost:4317`)
- `OTLP_INSECURE` (`true`/`false`)
- `CHUNK_SIZE_BYTES`
- `MAX_RETRIES`
- `MAX_INFLIGHT_CHUNKS_PER_UPLOAD`
- `MAX_FAIR_INFLIGHT_CHUNKS_PER_UPLOAD` (`0` = auto half of worker count)
- `MAX_GLOBAL_INFLIGHT_CHUNKS`
- `TASK_QUEUE_MAXSIZE`
- `WORKER_COUNT`
- `AUTOSCALE_ENABLED` (`true`/`false`)
- `MIN_WORKERS`
- `MAX_WORKERS`
- `AUTOSCALE_COOLDOWN_SECONDS`
- `SCALE_UP_QUEUE_THRESHOLD`
- `SCALE_UP_UTILIZATION_THRESHOLD`
- `SCALE_DOWN_UTILIZATION_THRESHOLD`
- `CLEANUP_ENABLED` (`true`/`false`)
- `CLEANUP_INTERVAL_SECONDS`
- `STALE_UPLOAD_TTL_SECONDS`
- `IDEMPOTENCY_TTL_SECONDS`

## Database Migrations
Apply latest schema:
```bash
alembic upgrade head
```

Rollback one revision:
```bash
alembic downgrade -1
```

Inside Docker app container:
```bash
docker compose exec app alembic upgrade head
```

## API
All `/v1/*` endpoints require authentication.
By default (`AUTH_MODE=api_key`): use `X-API-Key`.
When `AUTH_MODE=jwt`: use `Authorization: Bearer <token>`.
When `AUTH_MODE=hybrid`: JWT is preferred, API key fallback is accepted.

1. `POST /v1/uploads/init`
2. `PUT /v1/uploads/{upload_id}/chunks/{chunk_index}`
3. `POST /v1/uploads/{upload_id}/complete`
4. `GET /v1/uploads/{upload_id}/missing-chunks`
5. `GET /v1/uploads/{upload_id}/download`
6. `POST /v1/admin/cleanup` (authenticated maintenance trigger)

Checksum options:
- Send `file_checksum_sha256` in `POST /v1/uploads/init` to enforce end-to-end file checksum validation at complete time.
- Send `X-Chunk-SHA256` in chunk uploads to validate each chunk payload before writing.

Standard error payload:
```json
{
  "detail": "human-readable message",
  "error_code": "conflict",
  "request_id": "uuid-or-request-id",
  "upload_id": "optional-upload-id",
  "trace_id": "optional-opentelemetry-trace-id"
}
```

## Next Build Steps
1. Add additional migrations for upcoming schema changes.
2. Add S3-backed integration tests (real AWS environment).
3. Strengthen fairness policy in worker scheduling.

## Load Testing
Run a basic upload lifecycle load test (service must already be running):
```bash
python scripts/load_test.py --base-url http://127.0.0.1:8000 --files 10 --file-size-bytes 5242880 --chunk-size-bytes 1048576 --profile balanced --output benchmarks/results/baseline.json
```

Profiles:
- `fast`: lower concurrency, lower latency
- `balanced`: default
- `max-throughput`: more aggressive concurrency

You can still override profile values with:
- `--concurrent-files`
- `--per-file-chunk-workers`
- `--api-key` (default from `LOAD_TEST_API_KEY`, else `dev-key`)

Use `benchmarks/BASELINE_TEMPLATE.md` to record benchmark runs consistently.

## Monitoring Alerts
Sample Prometheus alert rules are provided in:
- `monitoring/alerts.yml`

They cover:
- high throttling rate
- chunk upload failure rate
- queue depth pressure
- worker saturation
- elevated API p95 latency

## Distributed Tracing
OpenTelemetry tracing can be enabled with OTLP export:
```bash
set TRACING_ENABLED=true
set TRACING_SERVICE_NAME=distributed-file-service
set OTLP_ENDPOINT=localhost:4317
set OTLP_INSECURE=true
```

When enabled:
- FastAPI requests are instrumented.
- Trace IDs are included in structured logs.
- Error responses include `trace_id` for correlation.

## AWS Setup Timing
You do not need AWS for local MVP development and tests.
Create and configure AWS when you begin:
1. Real S3 integration runs (`STORAGE_BACKEND=s3`)
2. End-to-end integration tests against actual S3
3. Staging deployment

## AWS Setup Checklist (When You Start)
Minimum setup before running real S3 integration:
1. Create AWS account and secure root user (MFA enabled).
2. Create an IAM user/role with S3 permissions limited to one test bucket/prefix.
3. Create an S3 bucket for this project (dev/test only).
4. Configure local credentials (`aws configure` or env vars).
5. Run optional integration test:
```bash
set RUN_AWS_INTEGRATION=1
set AWS_TEST_S3_BUCKET=<your-bucket>
set AWS_REGION=us-east-1
pytest -q tests/test_s3_integration_optional.py
```

## Cloudflare R2 Setup
If you are using R2 instead of AWS:
1. Create an R2 bucket in Cloudflare.
2. Create R2 API token (access key + secret).
3. Set env vars in PowerShell:
```bash
set STORAGE_BACKEND=r2
set R2_BUCKET=<your-r2-bucket>
set R2_ACCOUNT_ID=<your-account-id>
set R2_ACCESS_KEY_ID=<your-access-key>
set R2_SECRET_ACCESS_KEY=<your-secret-key>
```
4. Run service:
```bash
alembic upgrade head
uvicorn app.main:app --reload
```
5. Optional real R2 integration tests:
```bash
set RUN_R2_INTEGRATION=1
pytest -q tests/test_r2_integration_optional.py
pytest -q tests/test_api_r2_integration_optional.py
```
