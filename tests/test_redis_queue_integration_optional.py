import os
import uuid

import pytest
import httpx


RUN_REDIS_INTEGRATION = os.getenv("RUN_REDIS_INTEGRATION") == "1"
REDIS_INTEGRATION_BASE_URL = os.getenv("REDIS_INTEGRATION_BASE_URL", "http://127.0.0.1:8000")
REDIS_INTEGRATION_API_KEY = os.getenv("REDIS_INTEGRATION_API_KEY", "dev-key")


@pytest.mark.skipif(
    not RUN_REDIS_INTEGRATION,
    reason="Set RUN_REDIS_INTEGRATION=1 and run service with QUEUE_BACKEND=redis to execute this test.",
)
def test_redis_queue_upload_flow_live_server() -> None:
    file_bytes = b"redis-queue-live-flow"
    chunk_size = 5
    file_name = f"redis-live-{uuid.uuid4()}.bin"
    headers = {"X-API-Key": REDIS_INTEGRATION_API_KEY}

    with httpx.Client(base_url=REDIS_INTEGRATION_BASE_URL, timeout=20.0) as client:
        init = client.post(
            "/v1/uploads/init",
            json={"file_name": file_name, "file_size": len(file_bytes), "chunk_size": chunk_size},
            headers=headers,
        )
        assert init.status_code == 201, init.text
        upload_id = init.json()["upload_id"]

        chunks = [file_bytes[i : i + chunk_size] for i in range(0, len(file_bytes), chunk_size)]
        for index, payload in enumerate(chunks):
            response = client.put(
                f"/v1/uploads/{upload_id}/chunks/{index}",
                content=payload,
                headers={**headers, "Content-Length": str(len(payload))},
            )
            assert response.status_code == 202, response.text

        complete = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers)
        assert complete.status_code == 200, complete.text

        download = client.get(f"/v1/uploads/{upload_id}/download", headers=headers)
        assert download.status_code == 200, download.text
        assert download.content == file_bytes
