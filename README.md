# Distributed File Service

A service for uploading large files in chunks: resumable, checked chunk by chunk, verified as a whole, and safe to run as several instances. FastAPI, Postgres and S3-compatible storage (Amazon S3, Cloudflare R2 or local disk), with Redis or SQS for background verification and a browser UI.

![A 24 MiB upload through the UI: the plan, chunks confirmed by the server, three throttled chunks retried, and the completed upload](docs/demo.gif)

## Highlights

- **Resumable, verified uploads.** Every chunk is checked against its SHA-256, an interrupted upload resends only the chunks the server is missing, and an optional whole-file checksum is verified before the upload counts as complete ([tests](tests/test_api_flow.py), [tests](tests/test_finalizer.py)).
- **Safe under concurrency.** Racing writers of the same chunk, repeated requests and racing completions cannot corrupt an upload, and no database connection is held across a storage call ([tests](tests/test_chunk_races.py), [tests](tests/test_completion.py)).
- **Resilient to storage failures.** Transient errors are retried with backoff; outages answer `503` with `Retry-After` behind a circuit breaker ([tests](tests/test_storage_failures.py), [tests](tests/test_breaker.py)).
- **Runs as several instances** behind a load balancer, with a shared rate limit and separate verification workers ([tests](tests/test_multi_instance_optional.py)).
- **Secure by default.** Production refuses development credentials, other users' uploads look missing, errors do not leak internals, and dependencies are hash-locked and audited in CI ([tests](tests/test_secure_defaults.py)).
- **Measured, not guessed.** Pool sizes and download read-ahead were chosen from benchmarks, including on Amazon S3 ([report](benchmarks/results/2026-11-11_report.md)).

## Quick start

```bash
docker compose up -d --build app
```

Open <http://localhost:8000> and use the development API key `dev-key`. The compose stack runs in development mode; the container image on its own defaults to production and refuses that key.

## Usage

Every `/v1` request takes an `X-API-Key` header:

```text
POST /v1/uploads/init                   start: file name and size, optionally its SHA-256
PUT  /v1/uploads/{id}/chunks/{index}    send chunks in any order, each with X-Chunk-SHA256
POST /v1/uploads/{id}/complete          finish (202 VERIFYING while the whole file is checked)
GET  /v1/uploads/{id}/download          download, with Range; or POST .../download-url for a link
```

The [API guide](docs/api.md) has every endpoint, the error format and a curl walk-through.

## Performance

| Workload | Before this work | Now (median of 3) |
|---|---|---|
| 20 files of 64 MiB, 8 at a time | 0/20 uploads (deadlock) | 20/20, upload 95 MiB/s, download 170 MiB/s |
| 100 files of 16 MiB, all at once | 0/100 uploads | 100/100, upload 77 MiB/s |
| One 5 GiB file | not run | upload 75 MiB/s, download 147 MiB/s, SHA-256 verified |
| 20 files of 64 MiB on Amazon S3 (EC2, same region) | not run | upload 168 MiB/s, download 436 MiB/s |

Local numbers are from one machine with Docker Desktop; the [benchmark report](benchmarks/results/2026-11-11_report.md) has the method, the commands to reproduce them, and what limits throughput now.

## Documentation

- [API](docs/api.md): endpoints, errors, the upload flow, and the API changes in this version.
- [Configuration and deployment](docs/operations.md): settings, the container image, a production checklist, monitoring.
- [Design decisions and limitations](docs/design.md): why it is built this way, known limitations, future work.
- [Development](docs/development.md): tests, quality gates and project layout.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

CI runs linting, type checks, a dependency audit, the test suites on SQLite and Postgres, S3, Redis, two-instance and browser tests on every pull request.

## License

MIT, see [`LICENSE`](LICENSE).
