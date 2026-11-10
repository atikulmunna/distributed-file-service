# Development

Running the tests and quality gates, and where things live.

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
| S3-compatible storage ([tests](../tests/test_s3_integration_optional.py)) | `RUN_S3_INTEGRATION=1` | an S3 endpoint (CI runs the Versity gateway) |
| Redis job queue ([tests](../tests/test_redis_jobs_integration_optional.py)) | `RUN_REDIS_INTEGRATION=1` | Redis |
| Two instances ([tests](../tests/test_multi_instance_optional.py)) | `RUN_MULTI_INSTANCE=1` | `docker compose --profile cluster up -d --build nginx finalizer-cluster` |
| Browser smoke test ([tests](../tests/e2e/test_ui_smoke.py)) | `RUN_E2E=1` | `pip install -r requirements-e2e.txt` and `python -m playwright install chromium` |

CI ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) runs on every pull request: ruff, formatting and mypy; `pip-audit` over every locked package; the image checks; the SQLite suite with a coverage floor of 87% (line and branch); the Postgres suite with `alembic check`; and the S3, Redis, two-instance and browser suites.

**Dependencies** are pinned with hashes in `requirements.lock` (transitive ones included), resolved from `requirements.txt` with the command in the lock file's header; the image and CI install only from it.

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
