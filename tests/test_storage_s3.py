import sys
import types

from app.storage import S3ChunkStorage


class _FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "upload-xyz"}

    def upload_part(self, **kwargs):
        self.calls.append(("upload_part", kwargs))
        return {"ETag": '"etag-1"'}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        return {}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))
        return {}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": types.SimpleNamespace(read=lambda: b"abc")}


def test_s3_storage_multipart_flow(monkeypatch) -> None:
    fake_client = _FakeS3Client()
    fake_boto3 = types.SimpleNamespace(client=lambda service_name, region_name=None: fake_client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    storage = S3ChunkStorage(bucket="bucket-1", region="us-east-1")
    upload_id = storage.initialize_upload("u1")
    result = storage.write_chunk("u1", 0, b"abc", multipart_upload_id=upload_id)
    storage.complete_upload("u1", multipart_upload_id=upload_id, parts=[{"PartNumber": 1, "ETag": result.etag}])
    payload = storage.read_chunk(result.key)

    assert upload_id == "upload-xyz"
    assert result.key == "uploads/u1/chunk_0"
    assert result.etag == '"etag-1"'
    assert payload == b"abc"
    assert [name for name, _ in fake_client.calls] == [
        "create_multipart_upload",
        "upload_part",
        "put_object",
        "complete_multipart_upload",
        "get_object",
    ]
    assert fake_client.calls[0][1]["Key"] == "uploads/u1/assembled"
    assert fake_client.calls[1][1]["Key"] == "uploads/u1/assembled"
    assert fake_client.calls[2][1]["Key"] == "uploads/u1/chunk_0"
