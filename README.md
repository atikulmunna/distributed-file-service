# Distributed File Service

A service for uploading large files in chunks: resumable, checked chunk by chunk, verified as a whole, and safe to run as several instances. FastAPI, Postgres, S3-compatible storage (Amazon S3, Cloudflare R2, or local disk), Redis or SQS for background verification, and a browser UI that shows only what the server has confirmed.

![A 24 MiB upload through the UI: the plan, chunks confirmed by the server, three throttled chunks retried, and the completed upload](docs/demo.gif)

Every statement below links to the test or benchmark result that backs it.

## Contents

- [Quick start](#quick-start)
- [How an upload works](#how-an-upload-works)
- [API](#api)
- [The browser UI](#the-browser-ui)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Design decisions](#design-decisions)
- [Performance](#performance)
- [Testing and quality gates](#testing-and-quality-gates)
- [Known limitations](#known-limitations)
- [Future work](#future-work)
- [Project layout](#project-layout)

## Quick start

With Docker:

```bash
docker compose up -d --build app
```

This starts Postgres, Redis, a one-shot migration step, and the API on <http://localhost:8000>. Open it in a browser and enter the development API key `dev-key`. The compose stack runs with `APP_ENV=development`; the container image on its own defaults to production and refuses to start with that key ([Deployment](#deployment)).

Without Docker (SQLite and local disk storage):

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock
alembic upgrade head
uvicorn app.main:app --reload
```

## How an upload works

1. **Start** with the file's name and size (and optionally its SHA-256). The server answers with an upload ID and the chunk layout.
2. **Send the chunks** in any order, several at a time. Each chunk carries its SHA-256 and is rejected if the bytes do not match ([test](tests/test_api_flow.py)). Sending a chunk again is safe: the same content is a no-op ([test](tests/test_chunk_races.py)).
3. **Resume** after any interruption: the server lists which chunks it is missing, and only those are sent again ([test](tests/e2e/test_ui_smoke.py)).
4. **Complete.** The server assembles the parts. If a whole-file SHA-256 was declared, it answers `202 VERIFYING` and a background worker hashes the stored file; the upload becomes `COMPLETED` only if it matches, otherwise `FAILED` with the reason ([test](tests/test_finalizer.py)).
5. **Download** with `Range` support, or create a short-lived download link that needs no API key ([test](tests/test_download.py), [test](tests/test_signed_urls.py)).

The same flow with curl (a 5 MiB chunk size; on S3 every chunk but the last must be at least 5 MiB):

```bash
API=http://localhost:8000 KEY=dev-key FILE=report.pdf
SIZE=$(stat -c %s "$FILE")
BODY=$(printf '{"file_name": "%s", "file_size": %d, "chunk_size": 5242880}' "$FILE" "$SIZE")

# 1. start (the Idempotency-Key makes a repeated request return the same upload)
UPLOAD=$(curl -s -X POST "$API/v1/uploads/init" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: init-$FILE-$SIZE" --data "$BODY" | python -c 'import json,sys; print(json.load(sys.stdin)["upload_id"])')

# 2. send each chunk with its SHA-256
split -b 5242880 -d -a 4 "$FILE" chunk-
for part in chunk-*; do
  index=$((10#${part#chunk-}))
  curl -s -X PUT "$API/v1/uploads/$UPLOAD/chunks/$index" -H "X-API-Key: $KEY" \
    -H "X-Chunk-SHA256: $(sha256sum "$part" | cut -d' ' -f1)" -H "Idempotency-Key: chunk-$UPLOAD-$index" \
    --data-binary @"$part"
done

# 3. complete, 4. download (or ask for a link)
curl -s -X POST "$API/v1/uploads/$UPLOAD/complete" -H "X-API-Key: $KEY"
curl -s "$API/v1/uploads/$UPLOAD/download" -H "X-API-Key: $KEY" -o "copy-of-$FILE"
curl -s -X POST "$API/v1/uploads/$UPLOAD/download-url" -H "X-API-Key: $KEY"
```

`scripts/load_test.py` does the same in Python, with parallel chunks and retries.

## API

Every `/v1` endpoint takes `X-API-Key` (or `Authorization: Bearer <JWT>` with `AUTH_MODE=jwt` or `hybrid`). The OpenAPI document is served at `/docs`; the public surface is pinned by a test ([test](tests/test_openapi.py)).

| Method and path | What it does |
|---|---|
| `POST /v1/uploads/init` | Start an upload: `file_name`, `file_size`, optional `chunk_size` and `file_checksum_sha256`. `201` with the upload ID and layout. |
| `PUT /v1/uploads/{id}/chunks/{index}` | Send one chunk (`X-Chunk-SHA256`, `Idempotency-Key` optional). `202` once it is stored and recorded. |
| `POST /v1/uploads/{id}/complete` | Finish. `200 COMPLETED`, or `202 VERIFYING` when a whole-file checksum was declared. |
| `GET /v1/uploads/{id}` | Detail: status, chunk and byte counts, checksum result (`checksum_verified`), failure reason, timestamps. |
| `GET /v1/uploads/{id}/missing-chunks` | The chunk indexes the server has not received, for resuming. |
| `GET /v1/uploads/{id}/chunks?offset=&limit=` | Recorded chunks with size, SHA-256, status and storage attempts. |
| `GET /v1/uploads?status=&limit=&cursor=` | Your uploads, newest first, with keyset pagination. |
| `DELETE /v1/uploads/{id}` | Abort an unfinished upload and discard its stored data. |
| `GET /v1/uploads/{id}/download` | Download, with `Range` (`206`). |
| `POST /v1/uploads/{id}/download-url` | A link valid for `DOWNLOAD_URL_TTL_SECONDS` (300) that downloads without an API key. |
| `GET /v1/downloads/{token}` | Download by link, with `Range`. |
| `GET /v1/limits` | The bounds init enforces, so a client can choose a valid chunk size. |
| `GET /v1/stats` | This instance now and over the last 5 minutes (chunks, bytes, throttled requests, storage write p50 and p95). |
| `POST /v1/admin/cleanup` | Run cleanup now (admin users only). |
| `GET /health`, `GET /ready` | Liveness, and readiness (database, storage, queue) ([test](tests/test_ready.py)). |
| `GET /metrics`, `GET /version` | Prometheus metrics (optionally behind `METRICS_TOKEN`), and the version. |

**Errors** share one shape: `{"detail", "error_code", "request_id", "upload_id", "trace_id"}`; `422` adds `errors` (field location, message, type; the submitted value is not echoed). A `5xx` carries only `detail`, `error_code` and `request_id`, and the cause is logged under that request ID ([test](tests/test_secure_defaults.py)). `429` and `503` answers carry `Retry-After`.

**Ownership:** another user's upload answers `404`, exactly like a missing one, and idempotency keys are scoped to their owner ([test](tests/test_secure_defaults.py)).

### Changes in this version

- `POST /v1/uploads/{id}/complete` returns `202` with status `VERIFYING` when a whole-file checksum was declared (verification moved to a background worker).
- An upload owned by another user returns `404` instead of `403`.
- `422` responses use the error shape above, with `errors` instead of FastAPI's list-valued `detail`.
- `Idempotency-Key` is limited to 128 characters, and init and complete keys are scoped per user (keys stored before this change no longer match).
- `GET /v1/uploads/{id}` returns the full detail; `upload_id`, `status` and `failure_reason` are unchanged.
- New: `GET /v1/limits`, `GET /v1/uploads`, `GET /v1/uploads/{id}/chunks`, `GET /v1/stats`, `POST /v1/uploads/{id}/download-url`, `GET /v1/downloads/{token}`.

## The browser UI

Served at `/` as plain HTML, CSS and JavaScript, with no build step. Its rules: every number names its source (reported by an instance, or measured in this browser), the chunk map is colored only by what the server reports, and failures show the server's reason, the request ID and what happens next.

- Plans the upload from `/v1/limits` before sending; sends chunks in parallel with their SHA-256.
- Retries `429` and `503` after `Retry-After` (capped at 10 s) and other server or network errors with backoff, up to 5 attempts, with a countdown in the event list ([test](tests/e2e/test_ui_smoke.py)).
- Resumes after a reload: starting the same file finds its upload, and before resuming the page re-hashes a few chunks the server holds and refuses a different file with the same name ([test](tests/e2e/test_ui_smoke.py)).
- Hashes the whole file in a Web Worker in 8 MiB slices, so a large file is never held in memory ([test](tests/e2e/test_ui_smoke.py)).
- Downloads through a signed link, so the browser streams to disk.
- Runs under a strict Content-Security-Policy and never parses data as HTML ([test](tests/test_web_ui.py)).

| | |
|---|---|
| ![Transfer with throttled chunks retried](docs/screenshots/transfer-with-retries.png) | ![Completed upload with its detail](docs/screenshots/upload-completed.png) |
| ![Resume offer](docs/screenshots/resume-offer.png) | ![Dark theme](docs/screenshots/desktop-dark.png) |

## Configuration

Settings come from environment variables (or a `.env` file); the full list with comments is in [`app/config.py`](app/config.py). The ones most deployments set:

| Setting | Default | Meaning |
|---|---|---|
| `APP_ENV` | `development` (`production` in the image) | `production` refuses to start with development credentials. |
| `API_KEY_MAPPINGS` | `dev-key:dev-user` | `key:user` pairs, comma separated. |
| `ADMIN_USER_IDS` | `dev-user` | Users allowed to call the admin endpoint. |
| `AUTH_MODE`, `JWT_SECRET` | `api_key`, empty | `api_key`, `jwt` or `hybrid`; JWT secrets must be at least 32 bytes in production. |
| `DATABASE_URL` | SQLite file | Postgres in production: `postgresql+psycopg://...`. |
| `STORAGE_BACKEND` | `local` | `local`, `s3` (`S3_BUCKET`, `S3_ENDPOINT_URL`, `AWS_REGION`, `S3_SSE`) or `r2` (`R2_BUCKET`, `R2_ACCOUNT_ID`, keys). |
| `QUEUE_BACKEND`, `FINALIZER_IN_PROCESS` | `memory`, `true` | Verification jobs: `memory` (in process), `redis` or `sqs`; with a shared queue, run `python -m app.finalizer` separately. |
| `RATE_LIMIT_BACKEND`, `API_RATE_LIMIT_PER_MINUTE` | `memory`, `0` (off) | Per-user limit; `redis` shares one budget across instances. |
| `DOWNLOAD_URL_SECRET` | empty | Signs download links; required (32 bytes or more) in production and shared by all instances. |
| `DOWNLOAD_PREFETCH` | `4` | Ranged reads in flight per download; set `1` for storage on the same host ([why](#design-decisions)). |
| `WORKER_COUNT`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `16`, `10`, `10` | Storage threads and database connections per process, sized by measurement. |
| `MAX_FILE_SIZE_BYTES`, `MAX_CHUNK_SIZE_BYTES`, `MAX_TOTAL_CHUNKS` | 5 GiB, 64 MiB, 10000 | Input bounds ([test](tests/test_input_validation.py)). |
| `CLEANUP_ENABLED`, `STALE_UPLOAD_TTL_SECONDS`, `ORPHAN_GRACE_SECONDS` | `false`, 1 day, 1 hour | Periodic cleanup of abandoned uploads and orphaned objects. |
| `METRICS_TOKEN` | empty | When set, `/metrics` requires `Authorization: Bearer <token>`. |
| `INSTANCE_NAME` | hostname | Prefix of the instance ID in logs, headers, `/ready` and `/v1/stats`. |

## Deployment

**The image** ([`Dockerfile`](Dockerfile)) is built in two stages from the hash-locked dependencies, contains no compiler or tests, and runs as an unprivileged user. It defaults to `APP_ENV=production`, checks itself through `/ready`, and on `SIGTERM` gives open requests 30 s, then waits up to `SHUTDOWN_DRAIN_SECONDS` (10) for storage writes still running ([test](tests/test_runtime.py)); give containers a stop grace period of 45 s. CI checks that it runs as non-root, has no compiler, and refuses the development keys.

**Migrations** are a separate step: run `alembic upgrade head` once before starting the API, never from each replica. Compose does this in its `app-migrate` and `migrate` services.

**Production checklist**

- Set `APP_ENV=production` (the image does) and real values for `API_KEY_MAPPINGS`, `ADMIN_USER_IDS`, `DOWNLOAD_URL_SECRET`, and `JWT_SECRET` if JWT is used. The service refuses to start otherwise ([test](tests/test_secure_defaults.py)).
- Terminate TLS at the load balancer or proxy in front of the app.
- With several instances: `QUEUE_BACKEND=redis` (or `sqs`) with separate finalizer processes, `RATE_LIMIT_BACKEND=redis`, and the same `DOWNLOAD_URL_SECRET` everywhere.
- Postgres connections: each process may open `DB_POOL_SIZE + DB_MAX_OVERFLOW` (20 by default); keep `(API instances + finalizers) x 20` below Postgres `max_connections` (100 by default).
- Add a bucket rule that aborts incomplete multipart uploads, as a safety net beneath the service's own cleanup:

```json
{
  "Rules": [
    {
      "ID": "abort-incomplete-multipart-uploads",
      "Status": "Enabled",
      "Filter": {"Prefix": "uploads/"},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}
    }
  ]
}
```

```bash
aws s3api put-bucket-lifecycle-configuration --bucket <bucket> --lifecycle-configuration file://lifecycle.json
```

**Compose profiles** ([`docker-compose.yml`](docker-compose.yml)):

| Profile | What runs |
|---|---|
| default | Postgres, Redis, migrations, the API on port 8000 (reads `.env` if present). |
| `workers` | A separate verification worker (`python -m app.finalizer`) for `QUEUE_BACKEND=redis`. |
| `bench` | The benchmark stack: the API on port 8001 against an S3-compatible gateway (Versity) and its own database. |
| `cluster` | Two API instances behind nginx on port 8080, a separate verification worker, Redis-shared rate limit, and a migration step ([test](tests/test_multi_instance_optional.py)). |

**Monitoring:** Prometheus metrics at `/metrics`, alert rules in [`monitoring/alerts.yml`](monitoring/alerts.yml), a Grafana dashboard in [`monitoring/grafana`](monitoring/grafana), JSON request and audit logs with request IDs, and optional OpenTelemetry tracing (`TRACING_ENABLED`) ([test](tests/test_tracing.py)).

## Design decisions

**Concurrency.** Request handling is asynchronous; storage writes run in a fixed pool of 16 threads per process with bounded admission: when the queue or the global in-flight limit is full, the request gets `429` with `Retry-After` rather than waiting without bound ([test](tests/test_worker_pool.py)). No database connection is held across a storage call or while streaming a download ([test](tests/test_completion.py), [test](tests/test_download.py)); holding one is what deadlocked concurrent uploads in the original version. An earlier autoscaling thread pool was measured against a fixed pool and removed: it improved neither throughput nor p95 (W2 throughput -13.6%, p95 +15.9%), and 32 fixed workers were no better than 16 ([autoscaler](benchmarks/results/2026-11-10_autoscale_on-vs-off.json), [pool sizing](benchmarks/results/2026-11-10_pool-sizing.json)).

**Database and storage consistency.** Each chunk is written to storage once and then recorded with an upsert under a row lock, so racing writers of the same chunk and a racing completion cannot leave a half-recorded upload ([test](tests/test_chunk_races.py)). Completion claims the upload with a conditional update and a lease, so two completes cannot both assemble it and a crashed completion can be taken over ([test](tests/test_completion.py)). On S3, every part carries its SHA-256, which S3 verifies on receipt ([test](tests/test_s3_integration_optional.py)). Sizes and IDs are 64-bit ([test](tests/test_migrations.py)).

**Failure handling.** There is one retry layer: transient storage errors are retried with capped exponential backoff and full jitter, and boto's own retries are off ([test](tests/test_retry.py)). Persistent failures answer `503` with `Retry-After`, and after repeated failures a per-instance circuit breaker fails fast instead of tying up workers ([test](tests/test_storage_failures.py), [test](tests/test_breaker.py)).

**The queue** is used only for whole-file verification after completion, not on the chunk path. Delivery is at least once with leases: a job a crashed worker held is delivered again, the worker is idempotent, and a sweep re-enqueues uploads stuck in `VERIFYING` whose job was lost ([test](tests/test_jobs.py), [test](tests/test_finalizer.py), [Redis](tests/test_redis_jobs_integration_optional.py)).

**Cleanup cannot lose data.** An abandoned upload is first claimed as `ABORTED` with a conditional update that re-checks its inactivity, then its storage is cleaned, then its row is deleted; staleness is judged by the last activity, so a long upload that keeps sending chunks is never removed. Orphaned objects are deleted only after a grace period, a `VERIFYING` upload is never deleted, and a Postgres advisory lock keeps instances from cleaning at the same time ([test](tests/test_maintenance.py)).

**Per-instance and shared limits.** Backpressure (the storage pool, the per-upload in-flight limit) and the circuit breaker are per instance; the per-user rate limit is shared through Redis, keyed by user rather than by credential ([test](tests/test_ratelimit.py), [test](tests/test_multi_instance_optional.py)).

**Download read-ahead.** Each stored piece is read as ranged GETs of 8 MiB, 4 in flight. On real S3 from EC2 in the same region, one 1 GiB file downloaded at 76 MiB/s with one streaming GET per piece, 284 MiB/s with 4 in flight, and 321 or 364 MiB/s with 8 (at 72 or 144 MiB of buffers per download). On storage with near-zero latency, streaming was about 10% faster ([S3](benchmarks/results/2026-11-11_s3_download-prefetch.json), [local](benchmarks/results/2026-11-09_download_ab-task10-vs-task11.json)).

**Security defaults.** Production refuses development credentials and weak secrets; API keys and the metrics token are compared in constant time; signed download links are HMAC-signed, expire after 5 minutes, spend their owner's rate limit, and never appear in the app's logs, which record the route instead of the token ([test](tests/test_secure_defaults.py), [test](tests/test_signed_urls.py)); the reference [nginx config](deploy/nginx.conf) does the same.

## Performance

Measured on one Windows 11 machine with Docker Desktop (the server side in a 4-CPU VM) against an S3-compatible gateway, and on an EC2 c6i.xlarge against Amazon S3. The full report, with method, commands and the before and after table: [`benchmarks/results/2026-11-11_report.md`](benchmarks/results/2026-11-11_report.md).

| Workload | Before this work | Now (median of 3) |
|---|---|---|
| W1: 20 files of 64 MiB, 8 at a time | 0/20 uploads (deadlock) | 20/20, upload 95 MiB/s, download 170 MiB/s |
| W2: 100 files of 16 MiB, all at once | 0/100 uploads | 100/100, upload 77 MiB/s, chunk p95 14 s |
| W3: one 1 GiB file | upload 53 MiB/s | upload 84 MiB/s, download 138 MiB/s |
| One 5 GiB file | not run | upload 75 MiB/s, download 147 MiB/s, SHA-256 verified |
| W1 on Amazon S3 (EC2, same region) | not run | upload 168 MiB/s, download 436 MiB/s |

Every run had zero retries and zero errors. To reproduce the local numbers:

```bash
docker compose --profile bench up -d --build app-bench
python scripts/load_test.py --base-url http://localhost:8001 --workload W1   # or W2, W3
```

**What limits throughput now** (the report has the experiments): uploads are limited by the single app process (two instances raised W1 uploads by about 35% on the same VM, with CPU to spare); local download numbers are limited by Docker Desktop's port forwarding (about 440 MiB/s inside the Docker network against 170 through it); beyond 8 concurrent files, extra requests only wait.

## Testing and quality gates

```bash
pip install -r requirements-dev.txt
pytest                                    # SQLite and local storage
TEST_DATABASE_URL=postgresql+psycopg://dfs:dfs@localhost:5432/dfs_test pytest   # the same suite on Postgres
ruff check app tests scripts alembic && ruff format --check app tests scripts alembic && mypy
```

Opt-in suites, each enabled by a variable:

| Suite | Enable with | Needs |
|---|---|---|
| S3-compatible storage ([tests](tests/test_s3_integration_optional.py)) | `RUN_S3_INTEGRATION=1` | an S3 endpoint (CI runs the Versity gateway) |
| Redis job queue ([tests](tests/test_redis_jobs_integration_optional.py)) | `RUN_REDIS_INTEGRATION=1` | Redis |
| Two instances ([tests](tests/test_multi_instance_optional.py)) | `RUN_MULTI_INSTANCE=1` | `docker compose --profile cluster up -d --build nginx finalizer-cluster` |
| Browser smoke test ([tests](tests/e2e/test_ui_smoke.py)) | `RUN_E2E=1` | `pip install -r requirements-e2e.txt` and `python -m playwright install chromium` |

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every pull request: ruff, formatting and mypy; `pip-audit` over every locked package; the image checks; the SQLite suite with a coverage floor of 87% (line and branch); the Postgres suite with `alembic check`; and the S3, Redis, two-instance and browser suites.

**Dependencies** are pinned with hashes in `requirements.lock` (transitive ones included), resolved from `requirements.txt` with the command in the lock file's header; the image and CI install only from it.

## Known limitations

- A chunk's body is read into memory before the upload's in-flight slot is taken, so memory grows with concurrent chunk requests (2.46 GiB at 512 concurrent 8 MiB chunks in the saturation run). Nothing caps the total today.
- Backpressure limits and the circuit breaker are per instance; only the rate limit is shared.
- One region; no deduplication or storage tiering.
- TLS is not terminated by the app; put a proxy or load balancer in front.
- The SQS queue backend is tested with a fake client only, not against SQS itself. Server-side encryption settings are tested as request parameters (the local S3 gateway does not implement SSE).
- In the UI, chunk and whole-file checksums need a secure page (HTTPS or localhost).
- Local throughput figures depend on Docker Desktop on Windows; see the report for what that changes.

## Future work

- A byte budget for request bodies in flight, so memory has an upper bound under any load.
- Scaling instances on the `/v1/stats` signals.
- Uploads straight to S3 with presigned part URLs, keeping the service for coordination and verification.
- Multi-region replication, content-addressed deduplication, and tiering of old uploads to colder storage.
- TLS automation at the proxy.

## Project layout

```
app/                 the service
  routes/            HTTP endpoints: uploads, operations, the UI page
  static/            the browser UI (HTML, CSS, JavaScript, the hashing worker, vendored hash-wasm)
  chunk_service.py   chunk admission, validation, storage write and record
  upload_lifecycle.py  completion, verification hand-off, abort
  download.py        download planning and ranged reads
  storage.py         local, S3 and R2 storage
  storage_policy.py  retries and the circuit breaker
  jobs.py, finalizer.py  verification queue and worker
  maintenance.py     cleanup
alembic/             database migrations
tests/               unit and integration tests; tests/e2e is the browser test
scripts/             load test, benchmark setup, diagnostics
deploy/              nginx configuration for the cluster profile
monitoring/          Prometheus alerts and a Grafana dashboard
benchmarks/results/  every measurement referenced above
docs/                the demo GIF and screenshots
```
