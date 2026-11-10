"""One-shot setup for benchmark and integration stacks.

Creates the Postgres database named in DATABASE_URL and the bucket named in S3_BUCKET
(on S3_ENDPOINT_URL) if they do not exist yet. Waits up to 60 s for both services.
Credentials for S3 come from the standard AWS_* environment variables.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable


def ensure_postgres_database(url: str) -> bool:
    """Create the database named in `url` if missing. Returns True when it was created."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database})
            if exists:
                return False
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
            return True
    finally:
        admin.dispose()


def ensure_bucket(s3_client, bucket: str) -> bool:
    """Create `bucket` if missing. Returns True when it was created."""
    from botocore.exceptions import ClientError

    try:
        s3_client.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
            raise
    s3_client.create_bucket(Bucket=bucket)
    return True


def _retry(step: Callable[[], bool], what: str, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return step()
        except Exception as exc:  # services may still be starting
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{what} not ready after {timeout_s:.0f} s: {exc}") from exc
            time.sleep(1)


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    bucket = os.environ["S3_BUCKET"]
    endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None

    if database_url.startswith("postgresql"):
        created = _retry(lambda: ensure_postgres_database(database_url), "postgres")
        print(f"database: {'created' if created else 'exists'}")

    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}) if endpoint_url else None,
    )
    created = _retry(lambda: ensure_bucket(s3, bucket), "object storage")
    print(f"bucket {bucket}: {'created' if created else 'exists'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
