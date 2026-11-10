import os
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "distributed-file-service"
    app_version: str = "dev"
    # "production" refuses to start with development credentials (see validate_production_settings)
    # and trims what /version reveals. The container image sets APP_ENV=production.
    app_env: Literal["development", "production"] = "development"
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = "sqlite:///./distributed_file_service.db"
    storage_backend: str = "local"
    storage_root: str = "./data"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    # S3 verifies each part's SHA-256 on receipt. Turn off only for a provider that rejects checksum
    # headers (botocore may then still send its default CRC32; set AWS_REQUEST_CHECKSUM_CALCULATION=when_required).
    s3_part_checksums: bool = True
    s3_sse: str = ""  # "AES256" or "aws:kms"; empty uses the bucket default
    s3_sse_kms_key_id: str = ""
    aws_region: str = "us-east-1"
    r2_bucket: str = ""
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_endpoint_url: str = ""
    auth_mode: str = "api_key"
    api_key_mappings: str = "dev-key:dev-user"
    admin_user_ids: str = "dev-user"
    api_rate_limit_per_minute: int = 0
    # "memory": per instance; "redis": one budget per user shared by every instance (uses redis_url).
    rate_limit_backend: str = "memory"
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_audience: str = ""
    jwt_issuer: str = ""
    # When set, /metrics requires "Authorization: Bearer <metrics_token>".
    metrics_token: str = ""
    # Signs download links (POST /v1/uploads/{id}/download-url). Every instance must share it. When
    # empty in development, each process makes a random one (links then work only on that process).
    download_url_secret: str = ""
    download_url_ttl_seconds: int = 300
    tracing_enabled: bool = False
    tracing_service_name: str = "distributed-file-service"
    otlp_endpoint: str = "localhost:4317"
    otlp_insecure: bool = True
    chunk_size_bytes: int = 5 * 1024 * 1024
    # Input bounds. 5 GiB covers the spec's 5 GB files; 10,000 is S3's multipart part limit.
    max_file_size_bytes: int = 5 * 1024**3
    min_chunk_size_bytes: int = 1
    max_chunk_size_bytes: int = 64 * 1024**2
    max_total_chunks: int = 10_000
    chunk_body_timeout_seconds: int = 60
    # A COMPLETING upload whose claim is older than this may be taken over by another complete request.
    completion_lease_seconds: int = 60
    # Downloads read each stored piece as ranged GETs of download_segment_bytes, download_prefetch of
    # them in flight (memory about (prefetch + 1) segments per download); 1 means one streaming ranged
    # GET per piece. Real S3 from EC2 in the same region, one 1 GiB file (benchmarks/results/
    # 2026-11-11_s3_download-prefetch.json): prefetch 1 76 MiB/s, 4 284, 8 321, 8 with 16 MiB segments
    # 364. 4 takes the large step for 40 MiB per download. Storage with near-zero latency (on the same
    # host) favors 1 (benchmarks/results/2026-11-09_download_ab-task10-vs-task11.json).
    download_segment_bytes: int = 8 * 1024 * 1024
    download_prefetch: int = 4
    max_retries: int = 3  # retries after the first attempt, for transient storage errors only
    retry_base_delay_ms: int = 100
    retry_max_delay_ms: int = 2000
    # Storage circuit breaker (per instance): after this many consecutive transient storage failures,
    # storage calls fail immediately with 503 for breaker_open_seconds, then one probe call decides.
    breaker_failure_threshold: int = 5
    breaker_open_seconds: int = 10
    max_inflight_chunks_per_upload: int = 8
    max_fair_inflight_chunks_per_upload: int = 0
    max_global_inflight_chunks: int = 128
    task_queue_maxsize: int = 512
    # On shutdown, wait this long for storage writes still running (requests that already ended, for
    # example by client disconnect). Keep uvicorn's graceful timeout plus this under the container's
    # stop grace period.
    shutdown_drain_seconds: int = 10
    # Storage worker threads per instance, fixed. An autoscaler (8 to 32) improved neither throughput
    # nor p95 and was removed; 32 fixed workers were no better than 16 (benchmarks/results/
    # 2026-11-10_pool-sizing.json).
    worker_count: int = 16
    # SQLAlchemy pool for Postgres (SQLite keeps its own pool). Requests hold a connection only for
    # short transactions, never across storage calls. Measured on W2: 10/10 matched 20/20 on throughput
    # and p95 and beat 5/10 by about 4%. Each process may open pool_size + max_overflow connections;
    # keep (app instances + finalizers) x 20 under Postgres max_connections (100 by default).
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_s: int = 30
    # Job queue for post-completion verification: "memory" (in-process), "redis" (Streams), or "sqs".
    queue_backend: str = "memory"
    queue_poll_timeout_seconds: int = 5
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_name: str = "dfs:finalize"
    # A reserved job not acked within the lease is delivered again (its worker is presumed dead).
    job_lease_seconds: int = 300
    # Run verification workers inside the API process (needed for the memory queue), or not when
    # `python -m app.finalizer` runs them as a separate service.
    finalizer_in_process: bool = True
    finalizer_concurrency: int = 2
    finalizer_max_attempts: int = 5
    # How often a worker re-enqueues uploads stuck in VERIFYING (their job was lost).
    verification_sweep_seconds: int = 60
    sqs_queue_url: str = ""
    cleanup_enabled: bool = False
    cleanup_interval_seconds: int = 900
    stale_upload_ttl_seconds: int = 86400
    idempotency_ttl_seconds: int = 86400
    # An unreferenced stored object is deleted only once it is older than this: a chunk written to
    # storage but whose DB row is not committed yet must survive the orphan sweep.
    orphan_grace_seconds: int = 3600


AUTH_MODES = ("api_key", "jwt", "hybrid")
DEV_API_KEY = "dev-key"
DEV_ADMIN_USER = "dev-user"
MIN_JWT_SECRET_BYTES = 32
MIN_DOWNLOAD_URL_SECRET_BYTES = 32


def parse_api_key_mappings(raw: str) -> dict[str, str]:
    """ "key:user,key2:user2" -> {"key": "user", "key2": "user2"}; malformed items are skipped."""
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        api_key, sep, user_id = item.strip().partition(":")
        if sep and api_key.strip() and user_id.strip():
            mapping[api_key.strip()] = user_id.strip()
    return mapping


def validate_production_settings(s: Settings) -> list[str]:
    """Problems that make these settings unsafe to run in production (empty when safe)."""
    problems: list[str] = []
    mode = s.auth_mode.lower().strip()
    if mode not in AUTH_MODES:
        problems.append(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}")
    if mode in ("api_key", "hybrid") and DEV_API_KEY in parse_api_key_mappings(s.api_key_mappings):
        problems.append(f"API_KEY_MAPPINGS contains the public development key {DEV_API_KEY}")
    if DEV_ADMIN_USER in {item.strip() for item in s.admin_user_ids.split(",")}:
        problems.append(f"ADMIN_USER_IDS contains the development admin {DEV_ADMIN_USER}")
    if mode in ("jwt", "hybrid") and len(s.jwt_secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
        problems.append(f"JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes in {mode} mode")
    if len(s.download_url_secret.encode("utf-8")) < MIN_DOWNLOAD_URL_SECRET_BYTES:
        problems.append(f"DOWNLOAD_URL_SECRET must be at least {MIN_DOWNLOAD_URL_SECRET_BYTES} bytes")
    return problems


# DFS_ENV_FILE overrides which dotenv file is read; set it to an empty string to read none
# (the test suite does this so a developer .env can never point tests at real services).
settings = Settings(_env_file=os.getenv("DFS_ENV_FILE", ".env") or None)
