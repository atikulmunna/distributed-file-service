import sys
import types

from app.storage import S3ChunkStorage, build_storage, settings


def test_build_storage_r2_uses_cloudflare_endpoint(monkeypatch) -> None:
    calls: list[dict] = []

    class _FakeClient:
        pass

    def _fake_boto3_client(service_name, **kwargs):
        calls.append({"service_name": service_name, **kwargs})
        return _FakeClient()

    fake_boto3 = types.SimpleNamespace(client=_fake_boto3_client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    monkeypatch.setattr(settings, "storage_backend", "r2")
    monkeypatch.setattr(settings, "r2_bucket", "r2-bucket")
    monkeypatch.setattr(settings, "r2_account_id", "acct123")
    monkeypatch.setattr(settings, "r2_endpoint_url", "")
    monkeypatch.setattr(settings, "r2_access_key_id", "ak")
    monkeypatch.setattr(settings, "r2_secret_access_key", "sk")

    storage = build_storage()

    assert isinstance(storage, S3ChunkStorage)
    assert calls
    assert calls[0]["service_name"] == "s3"
    assert calls[0]["region_name"] == "auto"
    assert calls[0]["endpoint_url"] == "https://acct123.r2.cloudflarestorage.com"
    assert calls[0]["aws_access_key_id"] == "ak"
    assert calls[0]["aws_secret_access_key"] == "sk"
