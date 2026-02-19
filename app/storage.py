from pathlib import Path
from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class StorageWriteResult:
    key: str
    etag: str | None = None


class ChunkStorage:
    def initialize_upload(self, upload_id: str) -> str | None:
        return None

    def chunk_key(self, upload_id: str, chunk_index: int) -> str:
        raise NotImplementedError

    def write_chunk(
        self, upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None = None
    ) -> StorageWriteResult:
        raise NotImplementedError

    def complete_upload(self, upload_id: str, multipart_upload_id: str | None, parts: list[dict]) -> None:
        return None

    def read_chunk(self, s3_key: str) -> bytes:
        raise NotImplementedError

    def list_keys(self, prefix: str = "") -> list[str]:
        raise NotImplementedError

    def delete_key(self, key: str) -> None:
        raise NotImplementedError


class LocalChunkStorage(ChunkStorage):
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def chunk_key(self, upload_id: str, chunk_index: int) -> str:
        return f"uploads/{upload_id}/chunk_{chunk_index}"

    def write_chunk(
        self, upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None = None
    ) -> StorageWriteResult:
        relative_key = self.chunk_key(upload_id, chunk_index)
        full_path = self.root / relative_key
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(data)
        return StorageWriteResult(key=relative_key)

    def read_chunk(self, s3_key: str) -> bytes:
        return (self.root / s3_key).read_bytes()

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        root = self.root
        return [str(path.relative_to(root)).replace("\\", "/") for path in base.rglob("*") if path.is_file()]

    def delete_key(self, key: str) -> None:
        target = self.root / key
        if target.exists():
            target.unlink()


class S3ChunkStorage(ChunkStorage):
    def __init__(
        self,
        bucket: str,
        region: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket must be set for s3-compatible backends")
        import boto3

        self.bucket = bucket
        client_kwargs = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        if access_key_id and secret_access_key:
            client_kwargs["aws_access_key_id"] = access_key_id
            client_kwargs["aws_secret_access_key"] = secret_access_key
        self.client = boto3.client("s3", **client_kwargs)

    def chunk_key(self, upload_id: str, chunk_index: int) -> str:
        return f"uploads/{upload_id}/chunk_{chunk_index}"

    def _assembled_key(self, upload_id: str) -> str:
        return f"uploads/{upload_id}/assembled"

    def initialize_upload(self, upload_id: str) -> str | None:
        key = self._assembled_key(upload_id)
        result = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)
        return result["UploadId"]

    def write_chunk(
        self, upload_id: str, chunk_index: int, data: bytes, multipart_upload_id: str | None = None
    ) -> StorageWriteResult:
        key = self.chunk_key(upload_id, chunk_index)
        if not multipart_upload_id:
            put_result = self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
            return StorageWriteResult(key=key, etag=put_result.get("ETag"))

        assembled_key = self._assembled_key(upload_id)
        result = self.client.upload_part(
            Bucket=self.bucket,
            Key=assembled_key,
            PartNumber=chunk_index + 1,
            UploadId=multipart_upload_id,
            Body=data,
        )
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return StorageWriteResult(key=key, etag=result.get("ETag"))

    def complete_upload(self, upload_id: str, multipart_upload_id: str | None, parts: list[dict]) -> None:
        if not multipart_upload_id:
            raise ValueError("multipart_upload_id is required for s3 backend")
        key = self._assembled_key(upload_id)
        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=multipart_upload_id,
            MultipartUpload={"Parts": parts},
        )

    def read_chunk(self, s3_key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=s3_key)
        return obj["Body"].read()

    def list_keys(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        continuation_token = None
        while True:
            params = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation_token:
                params["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**params)
            for item in response.get("Contents", []):
                key = item.get("Key")
                if key:
                    keys.append(key)
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
        return keys

    def delete_key(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def build_storage() -> ChunkStorage:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalChunkStorage(settings.storage_root)
    if backend == "s3":
        return S3ChunkStorage(settings.s3_bucket, settings.aws_region)
    if backend == "r2":
        if not settings.r2_bucket:
            raise ValueError("r2_bucket must be set when storage_backend=r2")
        endpoint_url = settings.r2_endpoint_url
        if not endpoint_url:
            if not settings.r2_account_id:
                raise ValueError("set r2_endpoint_url or r2_account_id when storage_backend=r2")
            endpoint_url = f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"

        return S3ChunkStorage(
            bucket=settings.r2_bucket,
            region="auto",
            endpoint_url=endpoint_url,
            access_key_id=settings.r2_access_key_id or None,
            secret_access_key=settings.r2_secret_access_key or None,
        )
    raise ValueError(f"unsupported storage backend: {settings.storage_backend}")


storage = build_storage()
