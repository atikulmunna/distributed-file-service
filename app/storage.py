import base64
import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

# Errors worth retrying: throttling, server-side 5xx, timeouts, and connection failures. These are
# the cases botocore's standard retry mode retries; everything else (including local OSError such as a
# full disk) is permanent and fails fast.
TRANSIENT_ERROR_CODES = {
    "SlowDown",
    "RequestTimeout",
    "RequestTimeoutException",
    "InternalError",
    "ServiceUnavailable",
    "PriorRequestNotComplete",
    "Throttling",
    "ThrottlingException",
    "ThrottledException",
    "RequestThrottled",
    "RequestThrottledException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "BandwidthLimitExceeded",
    "ProvisionedThroughputExceededException",
}
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}


def is_transient_storage_error(exc: Exception) -> bool:
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        ConnectionError,
        EndpointConnectionError,
        ReadTimeoutError,
    )

    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in TRANSIENT_ERROR_CODES or status in TRANSIENT_HTTP_STATUSES
    # ConnectionError covers EndpointConnectionError and ConnectTimeoutError; the others are listed explicitly.
    return isinstance(exc, (ConnectionError, EndpointConnectionError, ConnectionClosedError, ReadTimeoutError))


@dataclass(frozen=True)
class StoredObject:
    key: str
    last_modified: datetime


@dataclass(frozen=True)
class StorageWriteResult:
    key: str
    etag: str | None = None


class ChunkStorage:
    def assembled_key(self, upload_id: str) -> str:
        """Key of the whole file once a multipart upload is completed."""
        return f"uploads/{upload_id}/assembled"

    def initialize_upload(self, upload_id: str) -> str | None:
        return None

    def chunk_key(self, upload_id: str, chunk_index: int) -> str:
        raise NotImplementedError

    def write_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        data: bytes,
        multipart_upload_id: str | None = None,
        sha256_hex: str | None = None,
    ) -> StorageWriteResult:
        """Store one chunk. `sha256_hex` (already computed by the caller) lets the backend verify it."""
        raise NotImplementedError

    def complete_upload(self, upload_id: str, multipart_upload_id: str | None, parts: list[dict]) -> None:
        return None

    def abort_upload(self, upload_id: str, multipart_upload_id: str | None) -> None:
        """Discard an unfinished multipart upload. No-op when there is none."""
        return None

    def object_exists(self, key: str) -> bool:
        raise NotImplementedError

    def check(self) -> None:
        """Raise if this backend cannot serve requests right now (used by /ready)."""
        raise NotImplementedError

    def iter_object(self, key: str, block_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        """Stream an object in blocks without loading it whole."""
        raise NotImplementedError

    def read_range(self, key: str, start: int, end: int) -> bytes:
        """Bytes start..end (inclusive) of a stored object."""
        raise NotImplementedError

    def iter_range(self, key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        """Stream bytes start..end (inclusive) of a stored object with a single read."""
        raise NotImplementedError

    def read_chunk(self, s3_key: str) -> bytes:
        raise NotImplementedError

    def list_keys(self, prefix: str = "") -> list[str]:
        raise NotImplementedError

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        """Objects under the prefix with their last-modified time (UTC)."""
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
        self,
        upload_id: str,
        chunk_index: int,
        data: bytes,
        multipart_upload_id: str | None = None,
        sha256_hex: str | None = None,
    ) -> StorageWriteResult:
        relative_key = self.chunk_key(upload_id, chunk_index)
        full_path = self.root / relative_key
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(data)
        return StorageWriteResult(key=relative_key)

    def read_chunk(self, s3_key: str) -> bytes:
        return (self.root / s3_key).read_bytes()

    def object_exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def check(self) -> None:
        probe = self.root / f".ready-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()

    def iter_object(self, key: str, block_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        with (self.root / key).open("rb") as handle:
            while block := handle.read(block_size):
                yield block

    def read_range(self, key: str, start: int, end: int) -> bytes:
        with (self.root / key).open("rb") as handle:
            handle.seek(start)
            return handle.read(end - start + 1)

    def iter_range(self, key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        with (self.root / key).open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0 and (block := handle.read(min(block_size, remaining))):
                remaining -= len(block)
                yield block

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        root = self.root
        return [str(path.relative_to(root)).replace("\\", "/") for path in base.rglob("*") if path.is_file()]

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        return [
            StoredObject(
                key=str(path.relative_to(self.root)).replace("\\", "/"),
                last_modified=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            )
            for path in base.rglob("*")
            if path.is_file()
        ]

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
        path_style: bool = False,
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
        from botocore.config import Config

        client_kwargs["config"] = Config(
            # Every worker thread can hold a connection; boto's default pool (10) is smaller than the pool.
            max_pool_connections=max(settings.worker_count, settings.max_workers) + 4,
            connect_timeout=5,
            read_timeout=60,
            # One retry layer: the app's policy (app/storage_policy.py), so attempts do not multiply.
            retries={"mode": "standard", "total_max_attempts": 1},
            s3={"addressing_style": "path"} if path_style else None,
        )
        self.client = boto3.client("s3", **client_kwargs)

    def chunk_key(self, upload_id: str, chunk_index: int) -> str:
        return f"uploads/{upload_id}/chunk_{chunk_index}"

    def _checksum_params(self, data: bytes, sha256_hex: str | None) -> dict:
        if not settings.s3_part_checksums:
            return {}
        digest = bytes.fromhex(sha256_hex) if sha256_hex else hashlib.sha256(data).digest()
        return {"ChecksumAlgorithm": "SHA256", "ChecksumSHA256": base64.b64encode(digest).decode("ascii")}

    def _sse_params(self) -> dict:
        if not settings.s3_sse:
            return {}
        params = {"ServerSideEncryption": settings.s3_sse}
        if settings.s3_sse == "aws:kms" and settings.s3_sse_kms_key_id:
            params["SSEKMSKeyId"] = settings.s3_sse_kms_key_id
        return params

    def initialize_upload(self, upload_id: str) -> str | None:
        key = self.assembled_key(upload_id)
        params = {"ChecksumAlgorithm": "SHA256"} if settings.s3_part_checksums else {}
        result = self.client.create_multipart_upload(Bucket=self.bucket, Key=key, **params, **self._sse_params())
        return result["UploadId"]

    def write_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        data: bytes,
        multipart_upload_id: str | None = None,
        sha256_hex: str | None = None,
    ) -> StorageWriteResult:
        checksum = self._checksum_params(data, sha256_hex)
        if not multipart_upload_id:  # a one-chunk upload is a plain object
            key = self.chunk_key(upload_id, chunk_index)
            put_result = self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **checksum, **self._sse_params())
            return StorageWriteResult(key=key, etag=put_result.get("ETag"))

        # One write per chunk: the part only. It has no object of its own; the chunk's key is the
        # assembled object it becomes part of, and its part number is chunk_index + 1.
        assembled_key = self.assembled_key(upload_id)
        result = self.client.upload_part(
            Bucket=self.bucket,
            Key=assembled_key,
            PartNumber=chunk_index + 1,
            UploadId=multipart_upload_id,
            Body=data,
            **checksum,
        )
        return StorageWriteResult(key=assembled_key, etag=result.get("ETag"))

    def complete_upload(self, upload_id: str, multipart_upload_id: str | None, parts: list[dict]) -> None:
        if not multipart_upload_id:
            raise ValueError("multipart_upload_id is required for s3 backend")
        key = self.assembled_key(upload_id)
        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=multipart_upload_id,
            MultipartUpload={"Parts": parts},
        )

    def abort_upload(self, upload_id: str, multipart_upload_id: str | None) -> None:
        if not multipart_upload_id:
            return
        from botocore.exceptions import ClientError

        try:
            self.client.abort_multipart_upload(
                Bucket=self.bucket, Key=self.assembled_key(upload_id), UploadId=multipart_upload_id
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("NoSuchUpload", "404"):
                raise

    def check(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def object_exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def read_chunk(self, s3_key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=s3_key)
        return obj["Body"].read()

    def iter_object(self, key: str, block_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        yield from body.iter_chunks(chunk_size=block_size)

    def read_range(self, key: str, start: int, end: int) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")
        return response["Body"].read()

    def iter_range(self, key: str, start: int, end: int, block_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        response = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")
        yield from response["Body"].iter_chunks(chunk_size=block_size)

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

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        objects: list[StoredObject] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            objects.extend(StoredObject(item["Key"], item["LastModified"]) for item in page.get("Contents", []))
        return objects

    def delete_key(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def build_storage() -> ChunkStorage:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalChunkStorage(settings.storage_root)
    if backend == "s3":
        # A custom endpoint (S3-compatible server) needs path-style addressing: bucket in the path,
        # not in the hostname. Credentials come from the standard AWS_* environment variables.
        return S3ChunkStorage(
            settings.s3_bucket,
            settings.aws_region,
            endpoint_url=settings.s3_endpoint_url or None,
            path_style=bool(settings.s3_endpoint_url),
        )
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
