"""Artifact-storage contract and deterministic in-memory implementation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..contracts import ArtifactRef, InspectionError
from ..contracts.core import require_text


class StorageError(RuntimeError):
    code = "ARTIFACT_STORAGE_ERROR"
    retryable = True

    def to_inspection_error(self) -> InspectionError:
        return InspectionError(
            code=self.code,
            stage="ARTIFACT_STORAGE",
            message=str(self),
            retryable=self.retryable,
        )


class StorageConflictError(StorageError):
    code = "ARTIFACT_CONFLICT"
    retryable = False


class ArtifactIntegrityError(StorageError):
    code = "ARTIFACT_CHECKSUM_MISMATCH"
    retryable = False


class ArtifactPolicyError(StorageError):
    code = "ARTIFACT_POLICY_VIOLATION"
    retryable = False


class ObjectNotFoundError(StorageError):
    code = "ARTIFACT_NOT_FOUND"
    retryable = True


class PutStatus(str, Enum):
    CREATED = "CREATED"
    ALREADY_PRESENT = "ALREADY_PRESENT"


@dataclass(frozen=True)
class ObjectMetadata:
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True)
class PutResult:
    status: PutStatus
    metadata: ObjectMetadata


class ArtifactStore(Protocol):
    def ensure_bucket(self, bucket: str) -> None: ...

    def validate_bucket(self, bucket: str) -> None: ...

    def head(self, bucket: str, key: str) -> ObjectMetadata | None: ...

    def put_if_absent(self, reference: ArtifactRef, content: bytes) -> PutResult: ...

    def get_verified(
        self, reference: ArtifactRef, *, max_bytes: int | None = None
    ) -> bytes: ...


class InMemoryArtifactStore:
    """Behavioral fake used by local integration and fault-injection tests."""

    def __init__(self) -> None:
        self._buckets: set[str] = set()
        self._objects: dict[tuple[str, str], tuple[ObjectMetadata, bytes]] = {}

    def ensure_bucket(self, bucket: str) -> None:
        self._buckets.add(require_text(bucket, "bucket"))

    def validate_bucket(self, bucket: str) -> None:
        if require_text(bucket, "bucket") not in self._buckets:
            raise ObjectNotFoundError("Artifact bucket does not exist.")

    def head(self, bucket: str, key: str) -> ObjectMetadata | None:
        stored = self._objects.get((bucket, key))
        return None if stored is None else stored[0]

    def put_if_absent(self, reference: ArtifactRef, content: bytes) -> PutResult:
        _verify_content(reference, content)
        if reference.bucket not in self._buckets:
            raise ObjectNotFoundError("Artifact bucket does not exist.")
        metadata = _metadata(reference)
        identity = (reference.bucket, reference.key)
        existing = self._objects.get(identity)
        if existing is not None:
            if existing[0] != metadata:
                raise StorageConflictError(
                    "Artifact key already exists with different immutable metadata."
                )
            return PutResult(PutStatus.ALREADY_PRESENT, existing[0])
        self._objects[identity] = (metadata, bytes(content))
        return PutResult(PutStatus.CREATED, metadata)

    def get_verified(
        self, reference: ArtifactRef, *, max_bytes: int | None = None
    ) -> bytes:
        stored = self._objects.get((reference.bucket, reference.key))
        if stored is None:
            raise ObjectNotFoundError("Artifact object does not exist.")
        metadata, content = stored
        _validate_remote_metadata(reference, metadata, max_bytes=max_bytes)
        _verify_content(reference, content)
        return bytes(content)


def _verify_content(reference: ArtifactRef, content: bytes) -> None:
    if not isinstance(content, bytes):
        raise ArtifactIntegrityError("Artifact content must be immutable bytes.")
    digest = hashlib.sha256(content).hexdigest()
    if digest != reference.sha256:
        raise ArtifactIntegrityError("Artifact checksum does not match its reference.")
    if reference.size_bytes is not None and len(content) != reference.size_bytes:
        raise ArtifactIntegrityError("Artifact size does not match its reference.")


def _metadata(reference: ArtifactRef) -> ObjectMetadata:
    if reference.size_bytes is None:
        raise ArtifactIntegrityError("Artifact reference requires size_bytes for storage.")
    return ObjectMetadata(
        bucket=reference.bucket,
        key=reference.key,
        sha256=reference.sha256,
        size_bytes=reference.size_bytes,
        content_type=reference.content_type,
    )


def _validate_remote_metadata(
    reference: ArtifactRef,
    metadata: ObjectMetadata,
    *,
    max_bytes: int | None,
) -> None:
    if metadata != _metadata(reference):
        raise ArtifactIntegrityError(
            "Stored artifact metadata does not match the requested reference."
        )
    if max_bytes is not None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValueError("max_bytes must be a positive integer")
        if metadata.size_bytes > max_bytes:
            raise ArtifactIntegrityError(
                "Stored artifact size exceeds the configured limit."
            )
