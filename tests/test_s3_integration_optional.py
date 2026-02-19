import os
import uuid

import pytest

from app.storage import S3ChunkStorage


RUN_AWS_INTEGRATION = os.getenv("RUN_AWS_INTEGRATION") == "1"
AWS_BUCKET = os.getenv("AWS_TEST_S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


@pytest.mark.skipif(
    not RUN_AWS_INTEGRATION or not AWS_BUCKET,
    reason="Set RUN_AWS_INTEGRATION=1 and AWS_TEST_S3_BUCKET to run real AWS integration tests.",
)
def test_s3_real_multipart_roundtrip() -> None:
    upload_id = f"it-{uuid.uuid4()}"
    storage = S3ChunkStorage(bucket=AWS_BUCKET, region=AWS_REGION)
    key = storage.chunk_key(upload_id, 0)

    multipart_upload_id = storage.initialize_upload(upload_id)
    write_result = storage.write_chunk(upload_id, 0, b"hello-aws", multipart_upload_id=multipart_upload_id)
    storage.complete_upload(
        upload_id=upload_id,
        multipart_upload_id=multipart_upload_id,
        parts=[{"PartNumber": 1, "ETag": write_result.etag}],
    )

    payload = storage.read_chunk(key)
    assert payload == b"hello-aws"

    storage.client.delete_object(Bucket=AWS_BUCKET, Key=key)
