# Distributed File Service (Phase 1 MVP)

This repository contains a runnable MVP backend for chunked uploads/downloads based on the simplified spec.

## Implemented
- Upload initialization
- Parallel chunk upload via bounded worker pool
- Retry loop for transient chunk write failures
- Resume support (`missing-chunks`)
- Upload completion validation
- Optional S3 multipart upload flow (`STORAGE_BACKEND=s3`)
- Download streaming with basic HTTP Range support
- Backpressure (`429`) on queue/global/per-upload inflight limits
- Prometheus metrics at `/metrics`

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
- `CHUNK_SIZE_BYTES`
- `MAX_RETRIES`
- `MAX_INFLIGHT_CHUNKS_PER_UPLOAD`
- `MAX_FAIR_INFLIGHT_CHUNKS_PER_UPLOAD` (`0` = auto half of worker count)
- `MAX_GLOBAL_INFLIGHT_CHUNKS`
- `TASK_QUEUE_MAXSIZE`
- `WORKER_COUNT`

## Database Migrations
Apply latest schema:
```bash
alembic upgrade head
```

Rollback one revision:
```bash
alembic downgrade -1
```

## API
All `/v1/*` endpoints require `X-API-Key` header.

1. `POST /v1/uploads/init`
2. `PUT /v1/uploads/{upload_id}/chunks/{chunk_index}`
3. `POST /v1/uploads/{upload_id}/complete`
4. `GET /v1/uploads/{upload_id}/missing-chunks`
5. `GET /v1/uploads/{upload_id}/download`

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
