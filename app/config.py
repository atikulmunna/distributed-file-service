import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "distributed-file-service"
    app_version: str = "dev"
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
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_audience: str = ""
    jwt_issuer: str = ""
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
    # download_prefetch = 1: each stored piece is read with one streaming ranged GET, in blocks of
    # download_segment_bytes. Above 1: pieces are split into ranged GETs of download_segment_bytes with
    # that many in flight (memory about (prefetch + 1) segments). Measured on the local bench stack
    # (near-zero storage latency): one streaming GET read 2233 MiB/s, 8 MiB ranged GETs 514 MiB/s at
    # prefetch 1 and 435 MiB/s at prefetch 4, so the default streams. Parallel ranged reads pay off on
    # high-latency storage; set the production value from a real-S3 measurement (Task 22).
    download_segment_bytes: int = 8 * 1024 * 1024
    download_prefetch: int = 1
    max_retries: int = 3  # retries after the first attempt, for transient storage errors only
    retry_base_delay_ms: int = 100
    retry_max_delay_ms: int = 2000
    max_inflight_chunks_per_upload: int = 8
    max_fair_inflight_chunks_per_upload: int = 0
    max_global_inflight_chunks: int = 128
    task_queue_maxsize: int = 512
    worker_count: int = 16
    autoscale_enabled: bool = False
    min_workers: int = 8
    max_workers: int = 32
    autoscale_cooldown_seconds: int = 15
    scale_up_queue_threshold: int = 1
    scale_up_utilization_threshold: float = 0.8
    scale_down_utilization_threshold: float = 0.2
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


# DFS_ENV_FILE overrides which dotenv file is read; set it to an empty string to read none
# (the test suite does this so a developer .env can never point tests at real services).
settings = Settings(_env_file=os.getenv("DFS_ENV_FILE", ".env") or None)
