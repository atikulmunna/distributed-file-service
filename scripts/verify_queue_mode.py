import os
import sys


def _ok(message: str) -> None:
    print(f"[OK] {message}")


def _fail(message: str) -> None:
    print(f"[FAIL] {message}")


def verify_memory() -> int:
    _ok("QUEUE_BACKEND=memory requires no external dependency.")
    return 0


def verify_redis() -> int:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        import redis
    except Exception as exc:
        _fail(f"redis package import failed: {exc}")
        return 1
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True)
        pong = client.ping()
        if pong is True:
            _ok(f"Connected to Redis at {redis_url}.")
            return 0
        _fail("Unexpected Redis ping response.")
        return 1
    except Exception as exc:
        _fail(f"Redis connectivity failed for {redis_url}: {exc}")
        return 1


def verify_sqs() -> int:
    sqs_queue_url = os.getenv("SQS_QUEUE_URL", "")
    aws_region = os.getenv("AWS_REGION", "")
    if not sqs_queue_url:
        _fail("SQS_QUEUE_URL is not set.")
        return 1
    if not aws_region:
        _fail("AWS_REGION is not set.")
        return 1
    try:
        import boto3
    except Exception as exc:
        _fail(f"boto3 import failed: {exc}")
        return 1

    try:
        client = boto3.client("sqs", region_name=aws_region)
        response = client.get_queue_attributes(
            QueueUrl=sqs_queue_url,
            AttributeNames=["QueueArn", "VisibilityTimeout"],
        )
        attrs = response.get("Attributes", {})
        queue_arn = attrs.get("QueueArn", "unknown")
        visibility = attrs.get("VisibilityTimeout", "unknown")
        _ok(f"SQS queue reachable: {queue_arn} (visibility_timeout={visibility})")
        return 0
    except Exception as exc:
        _fail(f"SQS connectivity/permission check failed: {exc}")
        return 1


def main() -> int:
    backend = os.getenv("QUEUE_BACKEND", "memory").lower()
    print(f"Verifying queue backend: {backend}")
    if backend == "memory":
        return verify_memory()
    if backend == "redis":
        return verify_redis()
    if backend == "sqs":
        return verify_sqs()
    _fail(f"Unsupported QUEUE_BACKEND value: {backend}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
