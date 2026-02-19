# High-Throughput Distributed File Upload and Download Service (Lite Spec)

## Project Description
Build a high-throughput backend for file upload and download using:
- Multithreaded chunk processing
- AWS S3 for object storage
- PostgreSQL for metadata
- Backpressure controls
- Observability metrics

This spec is intentionally split into two phases to keep delivery practical.

---

## Phase 1: Core MVP (Implement First)

### Goals
1. Chunk-based upload flow.
2. Parallel chunk upload with bounded worker pool.
3. Metadata persistence in PostgreSQL.
4. Resumable uploads.
5. Parallel download and file reconstruction.
6. Basic overload protection and metrics.

### Architecture
Client -> API Server -> PostgreSQL  
Client -> API Server -> S3  
API Server -> Worker Pool -> S3 + PostgreSQL

### Functional Requirements
1. Initialize upload:
- Generate `upload_id` (UUID)
- Store file metadata in PostgreSQL
- Return `upload_id`, `chunk_size`, `total_chunks`

2. Upload chunks:
- Client splits file into chunks
- Upload chunks in parallel
- Store chunk metadata (`upload_id`, `chunk_index`, `status`, `s3_key`)
- Retry failed chunks up to `MAX_RETRIES`

3. Complete upload:
- Verify all chunks uploaded
- Mark upload `COMPLETED`

4. Resume upload:
- Return missing chunk indexes for a given `upload_id`

5. Download:
- Retrieve chunk list
- Download chunks in parallel
- Reconstruct in order and stream to client
- Support HTTP Range requests (basic support is enough for MVP)

### Backpressure (MVP)
- Bounded task queue
- Per-upload inflight chunk limit
- Global inflight chunk limit
- Return HTTP `429` when limits are exceeded

### Database Schema (MVP)
`uploads`:
- `id UUID PRIMARY KEY`
- `file_name TEXT`
- `file_size BIGINT`
- `total_chunks INT`
- `status TEXT`
- `created_at TIMESTAMPTZ`

`chunks`:
- `id BIGSERIAL PRIMARY KEY`
- `upload_id UUID REFERENCES uploads(id)`
- `chunk_index INT`
- `s3_key TEXT`
- `status TEXT`
- `retry_count INT`
- `created_at TIMESTAMPTZ`

Recommended minimum constraint:
- `UNIQUE (upload_id, chunk_index)`

### Metrics (MVP)
Expose `/metrics`:
- `chunks_uploaded_total`
- `bytes_uploaded_total`
- `chunk_upload_failures_total`
- `retries_total`
- `task_queue_depth`
- `inflight_chunks`
- `throttled_requests_total`
- `worker_count`
- `worker_busy_count`

### Non-Functional Requirements
- Support 100+ concurrent uploads
- Support files up to 5 GB
- Retry transient S3 failures
- Use HTTPS and IAM-based S3 access

### Testing (MVP)
Unit tests:
- Chunk split logic
- Retry logic
- DB update logic

Integration tests:
- Full upload lifecycle
- Resume upload
- Parallel download

Load tests:
- Throughput
- Average upload latency
- Queue saturation behavior

---

## Phase 2: Production Hardening (After MVP)

### Reliability and Correctness
- Explicit upload state machine (`INITIATED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `ABORTED`)
- Idempotency keys for init/chunk/complete endpoints
- Stronger race handling around completion
- Checksum validation (chunk + full file)

### Adaptive Concurrency
- Dynamic worker scaling (`MIN_WORKERS` to `MAX_WORKERS`)
- Signals: queue depth, S3 latency, error rate, utilization
- Cooldown/hysteresis to avoid scaling oscillations

### Security and Governance
- Fine-grained AuthN/AuthZ on upload ownership
- S3 SSE-KMS
- Audit logging for upload/complete/download actions

### Observability Maturity
- p95/p99 latency histograms
- End-to-end request tracing
- SLOs and alerts (availability, latency, error budget)
- Grafana dashboards

### Scale Enhancements
- External durable queue (e.g., Redis/SQS)
- Horizontal worker autoscaling
- Multi-region/replication strategy
- Deduplication and storage tiering

---

## Performance Metrics to Benchmark
- Upload throughput (MB/s)
- Download throughput (MB/s)
- Average and p95 chunk latency
- Retry rate
- Queue saturation threshold
- Worker scaling behavior (Phase 2)

---

## Learning Outcomes
- Parallel file transfer design
- Backpressure and concurrency control
- S3 + PostgreSQL integration
- Reliability engineering under load
- Observability-driven performance tuning
