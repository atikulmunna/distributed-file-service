import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Chunk, ChunkRequestIdempotency, CompleteRequestIdempotency, InitRequestIdempotency, Upload
from app.storage import storage


RUN_R2_INTEGRATION = os.getenv("RUN_R2_INTEGRATION") == "1"
R2_BUCKET = os.getenv("R2_BUCKET", "")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")


@pytest.mark.skipif(
    not RUN_R2_INTEGRATION
    or not R2_BUCKET
    or not R2_ACCOUNT_ID
    or not R2_ACCESS_KEY_ID
    or not R2_SECRET_ACCESS_KEY,
    reason="Set RUN_R2_INTEGRATION=1 and R2_* env vars to run API-level Cloudflare R2 integration tests.",
)
def test_api_r2_full_upload_download_flow() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.execute(delete(ChunkRequestIdempotency))
        db.execute(delete(CompleteRequestIdempotency))
        db.execute(delete(InitRequestIdempotency))
        db.execute(delete(Chunk))
        db.execute(delete(Upload))
        db.commit()

    file_bytes = b"hello-r2-api-test"
    chunk_size = 6
    file_name = f"r2-api-{uuid.uuid4()}.bin"

    with TestClient(app) as client:
        init = client.post(
            "/v1/uploads/init",
            json={"file_name": file_name, "file_size": len(file_bytes), "chunk_size": chunk_size},
        )
        assert init.status_code == 201, init.text
        upload_id = init.json()["upload_id"]

        chunks = [file_bytes[i : i + chunk_size] for i in range(0, len(file_bytes), chunk_size)]
        for index, payload in enumerate(chunks):
            up = client.put(
                f"/v1/uploads/{upload_id}/chunks/{index}",
                content=payload,
                headers={"Content-Length": str(len(payload))},
            )
            assert up.status_code == 202, up.text

        complete = client.post(f"/v1/uploads/{upload_id}/complete")
        assert complete.status_code == 200, complete.text
        assert complete.json()["status"] == "COMPLETED"

        download = client.get(f"/v1/uploads/{upload_id}/download")
        assert download.status_code == 200, download.text
        assert download.content == file_bytes

        partial = client.get(f"/v1/uploads/{upload_id}/download", headers={"Range": "bytes=1-7"})
        assert partial.status_code == 206, partial.text
        assert partial.content == file_bytes[1:8]

    # Cleanup objects created during this integration test.
    for index in range(len(chunks)):
        storage.client.delete_object(Bucket=R2_BUCKET, Key=f"uploads/{upload_id}/chunk_{index}")
    storage.client.delete_object(Bucket=R2_BUCKET, Key=f"uploads/{upload_id}/assembled")
