import hashlib
from dataclasses import dataclass

import pytest

from label_inspection.contracts import ArtifactRef
from label_inspection.storage import (
    ArtifactIntegrityError,
    MinioArtifactStore,
    PutStatus,
    StorageConflictError,
)


@dataclass
class _Stat:
    size: int
    content_type: str
    metadata: dict[str, str]
    etag: str = "etag"


class _Response:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.closed = False
        self.released = False

        self.read_amounts: list[int | None] = []

    def read(self, amount: int | None = None) -> bytes:
        self.read_amounts.append(amount)
        if not self._content:
            return b""
        if amount is None:
            content, self._content = self._content, b""
            return content
        content, self._content = self._content[:amount], self._content[amount:]
        return content

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _S3Error(Exception):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class _FakeMinioClient:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.put_headers: list[dict[str, str]] = []
        self.get_calls = 0
        self.last_response: _Response | None = None

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def stat_object(self, bucket: str, key: str) -> _Stat:
        try:
            content, headers = self.objects[(bucket, key)]
        except KeyError as exc:
            raise _S3Error("NoSuchKey", 404) from exc
        return _Stat(
            size=len(content),
            content_type=headers["Content-Type"],
            metadata={name.lower(): value for name, value in headers.items()},
        )

    def _put_object(
        self, bucket: str, key: str, content: bytes, headers: dict[str, str]
    ) -> object:
        self.put_headers.append(dict(headers))
        identity = (bucket, key)
        if identity in self.objects and headers.get("If-None-Match") == "*":
            raise _S3Error("PreconditionFailed", 412)
        self.objects[identity] = (bytes(content), dict(headers))
        return object()

    def get_object(self, bucket: str, key: str) -> _Response:
        try:
            content, _ = self.objects[(bucket, key)]
        except KeyError as exc:
            raise _S3Error("NoSuchKey", 404) from exc
        self.get_calls += 1
        self.last_response = _Response(content)
        return self.last_response


def _ref(content: bytes) -> ArtifactRef:
    return ArtifactRef(
        bucket="vision-inspections",
        key="STATION-01/2026/08/25/event/source/label_crop.png",
        sha256=hashlib.sha256(content).hexdigest(),
        content_type="image/png",
        size_bytes=len(content),
    )


def test_minio_store_uses_atomic_create_only_put_and_is_idempotent():
    client = _FakeMinioClient()
    store = MinioArtifactStore(client)
    store.ensure_bucket("vision-inspections")
    content = b"exact-lossless-crop"
    reference = _ref(content)

    first = store.put_if_absent(reference, content)
    second = store.put_if_absent(reference, content)

    assert first.status is PutStatus.CREATED
    assert second.status is PutStatus.ALREADY_PRESENT
    assert client.put_headers[0]["If-None-Match"] == "*"
    assert client.put_headers[0]["X-Amz-Meta-Sha256"] == reference.sha256
    assert store.get_verified(reference) == content


def test_minio_store_never_overwrites_same_key_with_different_content():
    client = _FakeMinioClient()
    store = MinioArtifactStore(client)
    store.ensure_bucket("vision-inspections")
    original = _ref(b"original")
    store.put_if_absent(original, b"original")

    conflicting = _ref(b"different")
    with pytest.raises(StorageConflictError):
        store.put_if_absent(conflicting, b"different")

    assert client.objects[(original.bucket, original.key)][0] == b"original"


def test_minio_store_verifies_downloaded_bytes_not_only_remote_metadata():
    client = _FakeMinioClient()
    store = MinioArtifactStore(client)
    store.ensure_bucket("vision-inspections")
    reference = _ref(b"expected")
    store.put_if_absent(reference, b"expected")
    _, headers = client.objects[(reference.bucket, reference.key)]
    client.objects[(reference.bucket, reference.key)] = (b"tampered", headers)

    with pytest.raises(ArtifactIntegrityError):
        store.get_verified(reference)


def test_minio_store_fails_fast_if_pinned_conditional_primitive_is_missing():
    class UnsupportedClient:
        pass

    with pytest.raises(RuntimeError, match="conditional PUT"):
        MinioArtifactStore(UnsupportedClient())


def test_minio_rejects_oversized_head_before_object_download():
    client = _FakeMinioClient()
    store = MinioArtifactStore(client)
    store.ensure_bucket("vision-inspections")
    reference = _ref(b"12345678")
    store.put_if_absent(reference, b"12345678")
    client.get_calls = 0

    with pytest.raises(ArtifactIntegrityError, match="size"):
        store.get_verified(reference, max_bytes=4)

    assert client.get_calls == 0


def test_minio_rejects_wrong_content_type_before_object_download():
    client = _FakeMinioClient()
    store = MinioArtifactStore(client)
    store.ensure_bucket("vision-inspections")
    reference = _ref(b"expected")
    store.put_if_absent(reference, b"expected")
    content, headers = client.objects[(reference.bucket, reference.key)]
    headers = {**headers, "Content-Type": "application/octet-stream"}
    client.objects[(reference.bucket, reference.key)] = (content, headers)
    client.get_calls = 0

    with pytest.raises(ArtifactIntegrityError, match="metadata"):
        store.get_verified(reference, max_bytes=1024)

    assert client.get_calls == 0


def test_minio_bounded_stream_rejects_more_bytes_than_declared():
    client = _FakeMinioClient()
    store = MinioArtifactStore(client)
    store.ensure_bucket("vision-inspections")
    reference = _ref(b"expected")
    store.put_if_absent(reference, b"expected")
    original_get = client.get_object

    def return_extra(bucket, key):
        response = original_get(bucket, key)
        response._content += b"unexpected-extra"
        return response

    client.get_object = return_extra

    with pytest.raises(ArtifactIntegrityError, match="size"):
        store.get_verified(reference, max_bytes=1024)

    assert client.last_response is not None
    assert None not in client.last_response.read_amounts
