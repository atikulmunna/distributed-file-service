import base64
import hashlib
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
        payload = b"abcde"
        return {
            "Body": types.SimpleNamespace(
                read=lambda: payload,
                iter_chunks=lambda chunk_size: (payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)),
            )
        }


def _fake_storage(monkeypatch, **overrides) -> tuple[S3ChunkStorage, "_FakeS3Client"]:
    from app.config import settings

    for name, value in {"s3_part_checksums": True, "s3_sse": "", "s3_sse_kms_key_id": "", **overrides}.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    fake_client = _FakeS3Client()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda service_name, **kwargs: fake_client))
    return S3ChunkStorage(bucket="bucket-1", region="us-east-1"), fake_client


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def test_s3_storage_multipart_flow(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch)
    upload_id = storage.initialize_upload("u1")
    result = storage.write_chunk("u1", 0, b"abc", multipart_upload_id=upload_id)
    storage.complete_upload("u1", multipart_upload_id=upload_id, parts=[{"PartNumber": 1, "ETag": result.etag}])

    assert upload_id == "upload-xyz"
    assert result.key == "uploads/u1/assembled"  # a part has no object of its own
    assert result.etag == '"etag-1"'
    assert [name for name, _ in fake_client.calls] == [
        "create_multipart_upload",
        "upload_part",
        "complete_multipart_upload",
    ]
    assert all(kwargs["Key"] == "uploads/u1/assembled" for _, kwargs in fake_client.calls)


def test_multipart_write_makes_one_call_per_chunk(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch)
    result = storage.write_chunk("u1", 2, b"abc", multipart_upload_id="mp-1")
    assert [name for name, _ in fake_client.calls] == ["upload_part"]
    assert fake_client.calls[0][1]["PartNumber"] == 3
    assert fake_client.calls[0][1]["UploadId"] == "mp-1"
    assert result.key == "uploads/u1/assembled"


def test_part_checksum_sent(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch)
    storage.initialize_upload("u1")
    storage.write_chunk("u1", 0, b"abc", multipart_upload_id="mp-1", sha256_hex=hashlib.sha256(b"abc").hexdigest())
    storage.write_chunk("u2", 0, b"single")  # one-chunk upload: a plain object
    calls = dict((name, kwargs) for name, kwargs in fake_client.calls)
    assert calls["create_multipart_upload"]["ChecksumAlgorithm"] == "SHA256"
    assert calls["upload_part"]["ChecksumAlgorithm"] == "SHA256"
    assert calls["upload_part"]["ChecksumSHA256"] == _b64_sha256(b"abc")
    assert calls["put_object"]["ChecksumSHA256"] == _b64_sha256(b"single")  # computed when not passed in
    assert calls["put_object"]["Key"] == "uploads/u2/chunk_0"


def test_part_checksums_can_be_disabled(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch, s3_part_checksums=False)
    storage.initialize_upload("u1")
    storage.write_chunk("u1", 0, b"abc", multipart_upload_id="mp-1")
    storage.write_chunk("u2", 0, b"single")
    for _, kwargs in fake_client.calls:
        assert "ChecksumAlgorithm" not in kwargs and "ChecksumSHA256" not in kwargs


def test_sse_params_passed(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch, s3_sse="aws:kms", s3_sse_kms_key_id="key-123")
    storage.initialize_upload("u1")
    storage.write_chunk("u1", 0, b"abc", multipart_upload_id="mp-1")
    storage.write_chunk("u2", 0, b"single")
    calls = dict((name, kwargs) for name, kwargs in fake_client.calls)
    for name in ("create_multipart_upload", "put_object"):
        assert calls[name]["ServerSideEncryption"] == "aws:kms"
        assert calls[name]["SSEKMSKeyId"] == "key-123"
    assert "ServerSideEncryption" not in calls["upload_part"]  # parts inherit the upload's encryption

    storage, fake_client = _fake_storage(monkeypatch, s3_sse="AES256", s3_sse_kms_key_id="")
    storage.write_chunk("u3", 0, b"single")
    put = fake_client.calls[-1][1]
    assert put["ServerSideEncryption"] == "AES256" and "SSEKMSKeyId" not in put


def test_iter_object_streams_in_blocks(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch)
    blocks = list(storage.iter_object("uploads/u1/assembled", block_size=2))
    assert b"".join(blocks) == b"abcde"
    assert fake_client.calls[-1] == ("get_object", {"Bucket": "bucket-1", "Key": "uploads/u1/assembled"})


def test_build_storage_s3_uses_endpoint_url_with_path_style(monkeypatch) -> None:
    from app.storage import build_storage, settings

    calls: list[dict] = []
    fake_boto3 = types.SimpleNamespace(client=lambda service_name, **kwargs: calls.append(kwargs) or object())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "dfs", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint_url", "http://s3.local:7070", raising=False)

    storage = build_storage()

    assert isinstance(storage, S3ChunkStorage)
    assert storage.bucket == "dfs"
    assert calls[0]["endpoint_url"] == "http://s3.local:7070"
    assert calls[0]["config"].s3 == {"addressing_style": "path"}


def test_build_storage_s3_without_endpoint_uses_default_addressing(monkeypatch) -> None:
    from app.storage import build_storage, settings

    calls: list[dict] = []
    fake_boto3 = types.SimpleNamespace(client=lambda service_name, **kwargs: calls.append(kwargs) or object())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "dfs", raising=False)
    monkeypatch.setattr(settings, "s3_endpoint_url", "", raising=False)

    build_storage()

    assert "endpoint_url" not in calls[0]
    assert calls[0]["config"].s3 is None  # default (virtual-hosted) addressing


def test_iter_range_is_one_streaming_ranged_get(monkeypatch) -> None:
    storage, fake_client = _fake_storage(monkeypatch)
    blocks = list(storage.iter_range("uploads/u1/assembled", 1, 3, block_size=2))
    assert fake_client.calls[-1] == ("get_object", {"Bucket": "bucket-1", "Key": "uploads/u1/assembled", "Range": "bytes=1-3"})
    assert b"".join(blocks)  # streamed through iter_chunks
