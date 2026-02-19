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
    max_retries: int = 3
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
    queue_backend: str = "memory"
    queue_consumer_count: int = 4
    queue_poll_timeout_seconds: int = 5
    queue_task_timeout_seconds: int = 45
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_name: str = "dfs-chunk-tasks"
    sqs_queue_url: str = ""
    cleanup_enabled: bool = False
    cleanup_interval_seconds: int = 900
    stale_upload_ttl_seconds: int = 86400
    idempotency_ttl_seconds: int = 86400


settings = Settings()
