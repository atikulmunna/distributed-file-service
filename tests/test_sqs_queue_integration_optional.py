import os
import uuid

import httpx
import pytest


RUN_SQS_INTEGRATION = os.getenv("RUN_SQS_INTEGRATION") == "1"
SQS_INTEGRATION_BASE_URL = os.getenv("SQS_INTEGRATION_BASE_URL", "http://127.0.0.1:8000")
SQS_INTEGRATION_API_KEY = os.getenv("SQS_INTEGRATION_API_KEY", "dev-key")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
AWS_REGION = os.getenv("AWS_REGION", "")


@pytest.mark.skipif(
    not RUN_SQS_INTEGRATION or not SQS_QUEUE_URL or not AWS_REGION,
    reason=(
        "Set RUN_SQS_INTEGRATION=1, SQS_QUEUE_URL, and AWS_REGION; "
        "run service with QUEUE_BACKEND=sqs to execute this test."
    ),
)
def test_sqs_queue_upload_flow_live_server() -> None:
    file_bytes = b"sqs-queue-live-flow"
    chunk_size = 5
    file_name = f"sqs-live-{uuid.uuid4()}.bin"
    headers = {"X-API-Key": SQS_INTEGRATION_API_KEY}

    with httpx.Client(base_url=SQS_INTEGRATION_BASE_URL, timeout=30.0) as client:
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
