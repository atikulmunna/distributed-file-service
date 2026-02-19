import os
import uuid

import pytest

from app.storage import S3ChunkStorage


RUN_R2_INTEGRATION = os.getenv("RUN_R2_INTEGRATION") == "1"
R2_BUCKET = os.getenv("R2_BUCKET", "")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "")


@pytest.mark.skipif(
    not RUN_R2_INTEGRATION
    or not R2_BUCKET
    or not (R2_ENDPOINT_URL or R2_ACCOUNT_ID)
    or not R2_ACCESS_KEY_ID
    or not R2_SECRET_ACCESS_KEY,
    reason="Set RUN_R2_INTEGRATION=1 and R2_* env vars to run real Cloudflare R2 integration tests.",
)
def test_r2_real_multipart_roundtrip() -> None:
    upload_id = f"r2-it-{uuid.uuid4()}"
    endpoint = R2_ENDPOINT_URL or f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    storage = S3ChunkStorage(
        bucket=R2_BUCKET,
        region="auto",
        endpoint_url=endpoint,
        access_key_id=R2_ACCESS_KEY_ID,
        secret_access_key=R2_SECRET_ACCESS_KEY,
    )
    key = storage.chunk_key(upload_id, 0)

    multipart_upload_id = storage.initialize_upload(upload_id)
    write_result = storage.write_chunk(upload_id, 0, b"hello-r2", multipart_upload_id=multipart_upload_id)
    storage.complete_upload(
        upload_id=upload_id,
        multipart_upload_id=multipart_upload_id,
        parts=[{"PartNumber": 1, "ETag": write_result.etag}],
    )

    payload = storage.read_chunk(key)
    assert payload == b"hello-r2"

    storage.client.delete_object(Bucket=R2_BUCKET, Key=key)
