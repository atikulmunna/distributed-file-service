from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

chunks_uploaded_total = Counter("chunks_uploaded_total", "Total chunks uploaded")
bytes_uploaded_total = Counter("bytes_uploaded_total", "Total bytes uploaded")
chunk_upload_failures_total = Counter("chunk_upload_failures_total", "Total failed chunk uploads")
retries_total = Counter("retries_total", "Total retry attempts for chunk uploads")
throttled_requests_total = Counter("throttled_requests_total", "Total throttled requests")

task_queue_depth = Gauge("task_queue_depth", "Current task queue depth")
inflight_chunks = Gauge("inflight_chunks", "Current inflight chunk uploads")
worker_count = Gauge("worker_count", "Configured worker count")
worker_busy_count = Gauge("worker_busy_count", "Approximate busy workers")

s3_put_latency_seconds = Histogram("s3_put_latency_seconds", "Chunk storage write latency in seconds")
db_update_latency_seconds = Histogram("db_update_latency_seconds", "DB update latency in seconds")
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route", "status_code"],
)


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
