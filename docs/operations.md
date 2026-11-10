# Configuration and deployment

Settings, the container image, a production checklist, compose profiles and monitoring.

## Configuration

Settings come from environment variables (or a `.env` file); the full list with comments is in [`app/config.py`](../app/config.py). The ones most deployments set:

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
| `DOWNLOAD_PREFETCH` | `4` | Ranged reads in flight per download; set `1` for storage on the same host ([why](design.md#design-decisions)). |
| `WORKER_COUNT`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `16`, `10`, `10` | Storage threads and database connections per process, sized by measurement. |
| `MAX_FILE_SIZE_BYTES`, `MAX_CHUNK_SIZE_BYTES`, `MAX_TOTAL_CHUNKS` | 5 GiB, 64 MiB, 10000 | Input bounds ([test](../tests/test_input_validation.py)). |
| `CLEANUP_ENABLED`, `STALE_UPLOAD_TTL_SECONDS`, `ORPHAN_GRACE_SECONDS` | `false`, 1 day, 1 hour | Periodic cleanup of abandoned uploads and orphaned objects. |
| `METRICS_TOKEN` | empty | When set, `/metrics` requires `Authorization: Bearer <token>`. |
| `INSTANCE_NAME` | hostname | Prefix of the instance ID in logs, headers, `/ready` and `/v1/stats`. |

## Deployment

**The image** ([`Dockerfile`](../Dockerfile)) is built in two stages from the hash-locked dependencies, contains no compiler or tests, and runs as an unprivileged user. It defaults to `APP_ENV=production`, checks itself through `/ready`, and on `SIGTERM` gives open requests 30 s, then waits up to `SHUTDOWN_DRAIN_SECONDS` (10) for storage writes still running ([test](../tests/test_runtime.py)); give containers a stop grace period of 45 s. CI checks that it runs as non-root, has no compiler, and refuses the development keys.

**Migrations** are a separate step: run `alembic upgrade head` once before starting the API, never from each replica. Compose does this in its `app-migrate` and `migrate` services.

**Production checklist**

- Set `APP_ENV=production` (the image does) and real values for `API_KEY_MAPPINGS`, `ADMIN_USER_IDS`, `DOWNLOAD_URL_SECRET`, and `JWT_SECRET` if JWT is used. The service refuses to start otherwise ([test](../tests/test_secure_defaults.py)).
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

**Compose profiles** ([`docker-compose.yml`](../docker-compose.yml)):

| Profile | What runs |
|---|---|
| default | Postgres, Redis, migrations, the API on port 8000 (reads `.env` if present). |
| `workers` | A separate verification worker (`python -m app.finalizer`) for `QUEUE_BACKEND=redis`. |
| `bench` | The benchmark stack: the API on port 8001 against an S3-compatible gateway (Versity) and its own database. |
| `cluster` | Two API instances behind nginx on port 8080, a separate verification worker, Redis-shared rate limit, and a migration step ([test](../tests/test_multi_instance_optional.py)). |

**Monitoring:** Prometheus metrics at `/metrics`, alert rules in [`monitoring/alerts.yml`](../monitoring/alerts.yml), a Grafana dashboard in [`monitoring/grafana`](../monitoring/grafana), JSON request and audit logs with request IDs, and optional OpenTelemetry tracing (`TRACING_ENABLED`) ([test](../tests/test_tracing.py)).
