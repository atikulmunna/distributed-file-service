# Design decisions and limitations

Why the service is built the way it is, with the tests and measurements behind each decision, and what it does not do yet.

## Design decisions

**Concurrency.** Request handling is asynchronous; storage writes run in a fixed pool of 16 threads per process with bounded admission: when the queue or the global in-flight limit is full, the request gets `429` with `Retry-After` rather than waiting without bound ([test](../tests/test_worker_pool.py)). No database connection is held across a storage call or while streaming a download ([test](../tests/test_completion.py), [test](../tests/test_download.py)); holding one is what deadlocked concurrent uploads in the original version. An earlier autoscaling thread pool was measured against a fixed pool and removed: it improved neither throughput nor p95 (W2 throughput -13.6%, p95 +15.9%), and 32 fixed workers were no better than 16 ([autoscaler](../benchmarks/results/2026-11-10_autoscale_on-vs-off.json), [pool sizing](../benchmarks/results/2026-11-10_pool-sizing.json)).

**Database and storage consistency.** Each chunk is written to storage once and then recorded with an upsert under a row lock, so racing writers of the same chunk and a racing completion cannot leave a half-recorded upload ([test](../tests/test_chunk_races.py)). Completion claims the upload with a conditional update and a lease, so two completes cannot both assemble it and a crashed completion can be taken over ([test](../tests/test_completion.py)). On S3, every part carries its SHA-256, which S3 verifies on receipt ([test](../tests/test_s3_integration_optional.py)). Sizes and IDs are 64-bit ([test](../tests/test_migrations.py)).

**Failure handling.** There is one retry layer: transient storage errors are retried with capped exponential backoff and full jitter, and boto's own retries are off ([test](../tests/test_retry.py)). Persistent failures answer `503` with `Retry-After`, and after repeated failures a per-instance circuit breaker fails fast instead of tying up workers ([test](../tests/test_storage_failures.py), [test](../tests/test_breaker.py)).

**The queue** is used only for whole-file verification after completion, not on the chunk path. Delivery is at least once with leases: a job a crashed worker held is delivered again, the worker is idempotent, and a sweep re-enqueues uploads stuck in `VERIFYING` whose job was lost ([test](../tests/test_jobs.py), [test](../tests/test_finalizer.py), [Redis](../tests/test_redis_jobs_integration_optional.py)).

**Cleanup cannot lose data.** An abandoned upload is first claimed as `ABORTED` with a conditional update that re-checks its inactivity, then its storage is cleaned, then its row is deleted; staleness is judged by the last activity, so a long upload that keeps sending chunks is never removed. Orphaned objects are deleted only after a grace period, a `VERIFYING` upload is never deleted, and a Postgres advisory lock keeps instances from cleaning at the same time ([test](../tests/test_maintenance.py)).

**Per-instance and shared limits.** Backpressure (the storage pool, the per-upload in-flight limit) and the circuit breaker are per instance; the per-user rate limit is shared through Redis, keyed by user rather than by credential ([test](../tests/test_ratelimit.py), [test](../tests/test_multi_instance_optional.py)).

**Download read-ahead.** Each stored piece is read as ranged GETs of 8 MiB, 4 in flight. On real S3 from EC2 in the same region, one 1 GiB file downloaded at 76 MiB/s with one streaming GET per piece, 284 MiB/s with 4 in flight, and 321 or 364 MiB/s with 8 (at 72 or 144 MiB of buffers per download). On storage with near-zero latency, streaming was about 10% faster ([S3](../benchmarks/results/2026-11-11_s3_download-prefetch.json), [local](../benchmarks/results/2026-11-09_download_ab-task10-vs-task11.json)).

**Security defaults.** Production refuses development credentials and weak secrets; API keys and the metrics token are compared in constant time; signed download links are HMAC-signed, expire after 5 minutes, spend their owner's rate limit, and never appear in the app's logs, which record the route instead of the token ([test](../tests/test_secure_defaults.py), [test](../tests/test_signed_urls.py)); the reference [nginx config](../deploy/nginx.conf) does the same.

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
