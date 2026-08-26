"""MinIO-backed immutable artifact storage.

The official minio-py 7.2.20 public ``put_object`` API cannot attach an
``If-None-Match`` condition.  This adapter deliberately isolates its private
single-part ``_put_object`` primitive so object creation remains atomic instead
of relying on a racy HEAD-then-PUT sequence.  Construction fails fast when that
compatibility primitive is unavailable.
"""

from __future__ import annotations

from typing import Any

from ..contracts import ArtifactRef
from ..contracts.core import require_text
from .base import (
    ArtifactIntegrityError,
    ObjectMetadata,
    ObjectNotFoundError,
    PutResult,
    PutStatus,
    StorageConflictError,
    StorageError,
    _metadata,
    _validate_remote_metadata,
    _verify_content,
)

_NOT_FOUND_CODES = {"NoSuchBucket", "NoSuchKey", "NoSuchObject", "NotFound"}
_PRECONDITION_CODES = {
    "ConditionalRequestConflict",
    "PreconditionFailed",
    "NotModified",
}


class MinioArtifactStore:
    """ArtifactStore adapter with checksum verification and create-only PUT."""

    def __init__(self, client: Any) -> None:
        if not callable(getattr(client, "_put_object", None)):
            raise RuntimeError(  # noqa: TRY004 - pinned SDK compatibility failure
                "MinIO client does not expose the pinned conditional PUT primitive."
            )
        self._client = client

    @classmethod
    def connect(
        cls,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool = True,
    ) -> MinioArtifactStore:
        """Build the optional SDK client without making MinIO a core dependency."""

        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "MinIO support requires the phase2 optional dependencies."
            ) from exc
        return cls(
            Minio(
                endpoint=require_text(endpoint, "endpoint"),
                access_key=require_text(access_key, "access_key"),
                secret_key=require_text(secret_key, "secret_key"),
                secure=secure,
            )
        )

    def ensure_bucket(self, bucket: str) -> None:
        name = require_text(bucket, "bucket")
        try:
            if not self._client.bucket_exists(name):
                self._client.make_bucket(name)
        except Exception as exc:
            if _error_code(exc) not in {
                "BucketAlreadyExists",
                "BucketAlreadyOwnedByYou",
            }:
                raise StorageError("Artifact bucket could not be initialized.") from exc

    def validate_bucket(self, bucket: str) -> None:
        name = require_text(bucket, "bucket")
        try:
            exists = self._client.bucket_exists(name)
        except Exception as exc:
            raise StorageError("Artifact bucket readiness check failed.") from exc
        if not exists:
            raise ObjectNotFoundError(
                "Required artifact bucket is not provisioned."
            )

    def head(self, bucket: str, key: str) -> ObjectMetadata | None:
        name = require_text(bucket, "bucket")
        object_key = require_text(key, "key", max_length=1024)
        try:
            stat = self._client.stat_object(name, object_key)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise StorageError("Artifact metadata could not be read.") from exc
        metadata = _normalized_headers(getattr(stat, "metadata", {}))
        checksum = metadata.get("x-amz-meta-sha256")
        content_type = (
            getattr(stat, "content_type", None)
            or metadata.get("content-type")
            or "application/octet-stream"
        )
        size = getattr(stat, "size", None)
        if not isinstance(checksum, str) or not isinstance(size, int):
            raise ArtifactIntegrityError(
                "Stored artifact is missing immutable checksum metadata."
            )
        try:
            return ObjectMetadata(
                bucket=name,
                key=object_key,
                sha256=checksum.lower(),
                size_bytes=size,
                content_type=str(content_type),
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactIntegrityError(
                "Stored artifact metadata is invalid."
            ) from exc

    def put_if_absent(self, reference: ArtifactRef, content: bytes) -> PutResult:
        _verify_content(reference, content)
        headers = {
            "Content-Type": reference.content_type,
            "If-None-Match": "*",
            "X-Amz-Meta-Sha256": reference.sha256,
            "X-Amz-Meta-Size-Bytes": str(reference.size_bytes),
        }
        try:
            self._client._put_object(
                reference.bucket,
                reference.key,
                content,
                headers,
            )
        except Exception as exc:
            if not _is_precondition_failure(exc):
                raise StorageError("Artifact create-only upload failed.") from exc
            try:
                self.get_verified(reference)
            except (ArtifactIntegrityError, ObjectNotFoundError) as conflict:
                raise StorageConflictError(
                    "Artifact key already exists with different immutable content."
                ) from conflict
            return PutResult(PutStatus.ALREADY_PRESENT, _metadata(reference))

        # A successful HTTP response is not enough: read back and checksum the
        # exact bytes before allowing the spool state to advance.
        self.get_verified(reference)
        return PutResult(PutStatus.CREATED, _metadata(reference))

    def get_verified(
        self, reference: ArtifactRef, *, max_bytes: int | None = None
    ) -> bytes:
        remote = self.head(reference.bucket, reference.key)
        if remote is None:
            raise ObjectNotFoundError("Artifact object does not exist.")
        _validate_remote_metadata(reference, remote, max_bytes=max_bytes)
        response = None
        try:
            response = self._client.get_object(reference.bucket, reference.key)
        except Exception as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError("Artifact object does not exist.") from exc
            raise StorageError("Artifact object could not be downloaded.") from exc
        try:
            content = _read_bounded(response, expected_size=remote.size_bytes)
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                release = getattr(response, "release_conn", None)
                if callable(release):
                    release()
        if not isinstance(content, bytes):
            raise ArtifactIntegrityError("Artifact download did not return bytes.")
        _verify_content(reference, content)
        return content


def _error_code(exc: Exception) -> str:
    return str(getattr(exc, "code", ""))


def _is_not_found(exc: Exception) -> bool:
    return _error_code(exc) in _NOT_FOUND_CODES or getattr(exc, "status_code", None) == 404


def _is_precondition_failure(exc: Exception) -> bool:
    return (
        _error_code(exc) in _PRECONDITION_CODES
        or getattr(exc, "status_code", None) in {304, 409, 412}
    )


def _normalized_headers(headers: Any) -> dict[str, str]:
    try:
        return {str(name).lower(): str(value) for name, value in headers.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        raise ArtifactIntegrityError("Stored artifact metadata is unreadable.") from exc


def _read_bounded(response: Any, *, expected_size: int) -> bytes:
    remaining = expected_size + 1
    chunks: list[bytes] = []
    total = 0
    while remaining > 0:
        try:
            chunk = response.read(min(64 * 1024, remaining))
        except Exception as exc:
            raise StorageError("Artifact object could not be downloaded.") from exc
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ArtifactIntegrityError("Artifact download did not return bytes.")
        chunks.append(chunk)
        total += len(chunk)
        if total > expected_size:
            raise ArtifactIntegrityError(
                "Artifact stream size exceeds declared metadata."
            )
        remaining = expected_size + 1 - total
    content = b"".join(chunks)
    if len(content) != expected_size:
        raise ArtifactIntegrityError(
            "Artifact stream size does not match declared metadata."
        )
    return content
