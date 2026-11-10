from botocore.exceptions import ClientError

from scripts.bench_init import ensure_bucket


class _FakeS3:
    def __init__(self, existing: set[str]) -> None:
        self.buckets = set(existing)
        self.created: list[str] = []

    def head_bucket(self, Bucket: str) -> dict:
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")
        return {}

    def create_bucket(self, Bucket: str) -> dict:
        self.buckets.add(Bucket)
        self.created.append(Bucket)
        return {}


def test_ensure_bucket_creates_missing_bucket() -> None:
    s3 = _FakeS3(existing=set())
    assert ensure_bucket(s3, "dfs") is True
    assert s3.created == ["dfs"]


def test_ensure_bucket_keeps_existing_bucket() -> None:
    s3 = _FakeS3(existing={"dfs"})
    assert ensure_bucket(s3, "dfs") is False
    assert s3.created == []
